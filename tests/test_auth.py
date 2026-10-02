import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import httpx
from mcp.shared.auth import OAuthClientInformationFull
from mcp.server.auth.provider import AuthorizationParams, RegistrationError, TokenError
import pytest

from x_publisher.app import create_app
from x_publisher.auth import OwnerOAuth, callback_key
from x_publisher.core import DEFAULT_SCOPES, ISSUER, RESOURCE, SCOPES
from test_publisher import store, Backend, rpc

CALLBACK = "https://chatgpt.com/connector/oauth/publisher-test"


@pytest.mark.parametrize("url", [
    "http://localhost:43123/callback/Abcdefgh1234",
    "http://127.0.0.2:43123/callback/Abcdefgh1234",
    "http://127.0.0.1:43123/callback/Abcdefgh1234/extra",
    "http://127.0.0.1:43123/callback/Abcdefgh1234?next=evil",
    "http://user@127.0.0.1:43123/callback/Abcdefgh1234",
    "http://127.0.0.1:0/callback/Abcdefgh1234",
    "https://127.0.0.1:43123/callback/Abcdefgh1234",
])
def test_reject_unapproved_loopback_callback_shapes(url):
    assert callback_key(url) is None


async def test_codex_loopback_callback_requires_approved_path(store):
    path = "http://127.0.0.1/callback/Abcdefgh1234"
    actual = "http://127.0.0.1:43123/callback/Abcdefgh1234"
    assert callback_key(actual) == path
    store.set_setting("callbacks", [path])
    app = create_app(store, "owner-" + "x" * 60, Backend)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://mcp.example.test",
            follow_redirects=False) as client:
        md = (await client.get("/.well-known/oauth-authorization-server/x-mcp/oauth")).json()
        body = {"redirect_uris": [actual], "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}
        registered = await client.post(md["registration_endpoint"], json=body)
        assert registered.status_code == 201, registered.text
        rejected = await client.post(md["registration_endpoint"], json={
            **body, "redirect_uris": ["http://127.0.0.1:43123/callback/WrongPath1234"]})
        assert rejected.status_code == 400
        params = {"client_id": registered.json()["client_id"], "redirect_uri": actual,
                  "response_type": "code", "code_challenge": "v" * 43,
                  "code_challenge_method": "S256", "scope": "x:read", "resource": RESOURCE,
                  "state": "codexstate"}
        authorization = await client.get(md["authorization_endpoint"], params=params)
        assert authorization.status_code in (302, 303, 307)
        assert authorization.headers["location"].startswith(ISSUER + "/consent?")


async def test_oauth_scopes_refresh_revocation(store):
    oauth = OwnerOAuth(store, "a"*64)
    store.set_setting("callbacks", [CALLBACK])
    client = OAuthClientInformationFull(client_id="test",redirect_uris=[CALLBACK],token_endpoint_auth_method="none")
    await oauth.register_client(client)
    bad = OAuthClientInformationFull(client_id="bad",redirect_uris=["https://attacker.example/cb"],token_endpoint_auth_method="none")
    with pytest.raises(RegistrationError):
        await oauth.register_client(bad)
    issued = oauth.issue("test", ["publisher:status"], ["1"])
    assert oauth.principal(issued.access_token).accounts == frozenset({"1"})
    refresh = await oauth.load_refresh_token(client, issued.refresh_token)
    with pytest.raises(TokenError):
        await oauth.exchange_refresh_token(client, refresh, SCOPES)
    issued = oauth.issue("test",SCOPES,["1"])
    refresh = await oauth.load_refresh_token(client, issued.refresh_token)
    rotated = await oauth.exchange_refresh_token(client,refresh,["publisher:status"])
    assert not oauth.principal(issued.access_token)
    assert oauth.principal(rotated.access_token).accounts == frozenset({"1"})
    assert oauth.principal(rotated.access_token).scopes == frozenset({"publisher:status"})
    await oauth.revoke_token(await oauth.load_access_token(rotated.access_token))
    assert not oauth.principal(rotated.access_token)
    oauth.db.close()


@pytest.mark.parametrize("requested_scopes,approved_scopes,selected_accounts", [
    (DEFAULT_SCOPES, [], []),
    (SCOPES, ["buffer:publish", "publisher:publish", "cleanup:execute", "cleanup:protect"], ["1"]),
])
async def test_full_oauth_pkce_consent_and_replay(store, requested_scopes, approved_scopes, selected_accounts):
    store.set_setting("callbacks",[CALLBACK])
    app = create_app(store,"owner-"+"x"*60,Backend)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="https://mcp.example.test",follow_redirects=False) as c:
        md = (await c.get("/.well-known/oauth-authorization-server/x-mcp/oauth")).json()
        assert md["issuer"] == ISSUER
        registration = await c.post(md["registration_endpoint"],json={"redirect_uris":[CALLBACK],"token_endpoint_auth_method":"none","grant_types":["authorization_code","refresh_token"],"response_types":["code"]})
        assert registration.status_code == 201, registration.text
        client = registration.json()["client_id"]
        verifier = "v"*64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        assert registration.json()["scope"] == " ".join(DEFAULT_SCOPES)
        params = {"client_id":client,"redirect_uri":CALLBACK,"response_type":"code","code_challenge":challenge,"code_challenge_method":"S256","scope":" ".join(requested_scopes),"resource":RESOURCE,"state":"ownerstate"}
        # A read-only registration may request new scopes, subject to owner consent.
        upgraded = await c.get(md["authorization_endpoint"], params={**params,"scope":" ".join(SCOPES)})
        assert upgraded.status_code in (302,303,307), upgraded.text
        assert "error=" not in upgraded.headers["location"]
        upgrade_page = await c.get(upgraded.headers["location"])
        assert 'name="approve_scope" value="cleanup:protect"' in upgrade_page.text
        unknown = await c.get(md["authorization_endpoint"], params={**params,"scope":"admin:all"})
        assert "invalid_scope" in unknown.headers["location"]
        authorization = await c.get(md["authorization_endpoint"],params=params)
        assert authorization.status_code in (302,303,307), authorization.text
        consent_url = authorization.headers["location"]
        pending = parse_qs(urlsplit(consent_url).query)["request"][0]
        consent = await c.get(consent_url)
        assert consent.status_code == 200
        assert "owner-" not in consent.text
        assert ('name="accounts"' in consent.text) == bool(selected_accounts)
        assert ('name="approve_scope" value="publisher:publish"' in consent.text) == ("publisher:publish" in requested_scopes)
        approved = await c.post("/x-mcp/oauth/consent",data={"request":pending,"key":"owner-"+"x"*60,
            "accounts":selected_accounts,"approve_scope":approved_scopes},headers={"Origin":"https://mcp.example.test"})
        assert approved.status_code == 303, approved.text
        values = parse_qs(urlsplit(approved.headers["location"]).query)
        assert values["state"] == ["ownerstate"]
        body = {"grant_type":"authorization_code","code":values["code"][0],"client_id":client,"redirect_uri":CALLBACK,"code_verifier":verifier,"resource":RESOURCE}
        wrong_verifier = await c.post(md["token_endpoint"],data={**body,"code_verifier":"wrong"*16})
        assert wrong_verifier.status_code == 400
        tokens = await c.post(md["token_endpoint"],data=body)
        assert tokens.status_code == 200, tokens.text
        token = tokens.json()["access_token"]
        expected_scopes = set(requested_scopes)
        assert set(tokens.json()["scope"].split()) == expected_scopes
        repeated = await c.get(md["authorization_endpoint"], params={**params,"scope":" ".join(sorted(expected_scopes))})
        assert repeated.status_code in (302,303,307), repeated.text
        assert "error=" not in repeated.headers["location"]
        refresh_body = {"grant_type":"refresh_token", "client_id":client, "refresh_token":tokens.json()["refresh_token"]}
        refreshed = await c.post(md["token_endpoint"], data=refresh_body)
        assert refreshed.status_code == 200, refreshed.text
        assert set(refreshed.json()["scope"].split()) == expected_scopes
        token = refreshed.json()["access_token"]
        result = (await rpc(c,token,"tools/call",{"name":"publishing_status","arguments":{}})).json()["result"]
        if selected_accounts:
            assert [a["account_id"] for a in result["structuredContent"]["accounts"]] == ["1"]
        else:
            assert result["isError"] and "insufficient_scope" in result["content"][0]["text"]
        cleanup = (await rpc(c,token,"tools/call",{"name":"cleanup_status","arguments":{"account_id":"1"}})).json()["result"]
        if selected_accounts:
            assert not cleanup.get("isError"), cleanup
        else:
            assert cleanup["isError"] and "insufficient_scope" in cleanup["content"][0]["text"]
        next_consent = await c.get(repeated.headers["location"])
        next_pending = parse_qs(urlsplit(repeated.headers["location"]).query)["request"][0]
        invalid = await c.post("/x-mcp/oauth/consent",data={"request":next_pending,"key":"owner-"+"x"*60,"accounts":"1","approve_scope":"admin:all"},headers={"Origin":"https://mcp.example.test"})
        assert invalid.status_code == 400 and invalid.json()["error"] == "invalid_scope"
        replay = await c.post(md["token_endpoint"],data=body)
        assert replay.status_code == 400
        assert (await c.post("/x-mcp/oauth/consent",data={"request":pending,"key":"owner-"+"x"*60,"accounts":"1"},headers={"Origin":"https://attacker.example"})).status_code == 403


async def test_upload_authorization_and_bad_origin(store):
    from test_publisher import png
    token = store.issue_token("test",SCOPES,["1"])
    token2 = store.issue_token("other",SCOPES,["2"])
    app = create_app(store,"x"*64,Backend)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="https://mcp.example.test") as c:
        data = png()
        response = await rpc(c,token,"tools/call",{"name":"begin_media_upload","arguments":{"account_id":"1","name":"a.png","size":len(data)}})
        upload = response.json()["result"]["structuredContent"]
        url = upload["upload_url"]
        headers = {"Authorization":"Bearer "+token,"Upload-Offset":"0"}
        assert (await c.put(url,headers={**headers,"Authorization":"Bearer "+token2},content=data)).status_code == 403
        assert (await c.put(url,headers={**headers,"Origin":"https://attacker.example"},content=data)).status_code == 400
        assert (await c.put(url,headers=headers,content=data)).status_code == 200
        result = await c.post(url,headers=headers)
        assert result.status_code == 200 and result.json()["kind"] == "image"
        badhost = await c.get(url,headers={**headers,"Host":"attacker.example"})
        assert badhost.status_code == 400
