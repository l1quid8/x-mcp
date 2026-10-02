"""Buffer credentials and grants stay isolated from legacy X sessions."""
import os
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from x_publisher.app import create_app
from x_publisher.admin import verified_buffer_channels
from x_publisher.auth import OwnerOAuth
from x_publisher.core import ORIGIN, RESOURCE, Problem
from test_publisher import Backend, store


CHANNEL = {"account_id": "buffer:channel_123", "channel_id": "channel_123",
           "display_name": "Example account", "handle": "example"}


def test_buffer_key_is_encrypted_private_and_rotation_revokes_channels(store):
    assert store.buffer_key() is None
    secret = "buffer-secret-that-must-never-appear-on-disk"
    store.save_buffer_key(secret)
    path = store.directory / "buffer-key.enc"
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert secret.encode() not in path.read_bytes()
    assert secret.encode() not in (store.directory / "publisher.sqlite3").read_bytes()
    assert store.buffer_key() == secret

    store.save_buffer_channels([CHANNEL])
    assert store.buffer_channel(CHANNEL["account_id"]) == CHANNEL
    store.save_buffer_key("replacement-secret")
    assert store.buffer_channels() == []
    with pytest.raises(Problem, match="configured Buffer X channel"):
        store.buffer_channel(CHANNEL["account_id"])


def test_buffer_grants_are_provider_scoped(store):
    store.save_buffer_key("test-buffer-key")
    store.save_buffer_channels([CHANNEL])
    token = store.issue_token("buffer", ["buffer:status", "buffer:publish"], [CHANNEL["account_id"]])
    principal = store.token_principal(token)
    assert principal.scopes == frozenset({"buffer:status", "buffer:publish"})
    assert principal.accounts == frozenset({CHANNEL["account_id"]})

    with pytest.raises(ValueError, match="Buffer scope"):
        store.issue_token("legacy", ["publisher:publish"], [CHANNEL["account_id"]])
    with pytest.raises(ValueError, match="publisher or cleanup scope"):
        store.issue_token("wrong", ["buffer:publish"], ["1"])
    with pytest.raises(Problem, match="configured Buffer X channel"):
        store.issue_token("missing", ["buffer:publish"], ["buffer:unknown"])

    oauth = OwnerOAuth(store, "owner-" + "x" * 60)
    with pytest.raises(ValueError, match="Buffer scope"):
        oauth.issue("client", ["publisher:publish"], [CHANNEL["account_id"]])
    granted = oauth.issue("client", ["buffer:publish"], [CHANNEL["account_id"]])
    assert oauth.principal(granted.access_token).accounts == frozenset({CHANNEL["account_id"]})
    oauth.db.close()


def test_channel_list_rejects_duplicate_and_mismatched_ids(store):
    store.save_buffer_key("test-buffer-key")
    with pytest.raises(ValueError, match="identifier"):
        store.save_buffer_channels([CHANNEL, CHANNEL])
    with pytest.raises(ValueError, match="identifier"):
        store.save_buffer_channels([{**CHANNEL, "account_id": "1"}])
    assert store.buffer_channels() == []


def test_verified_buffer_x_identity_is_stored_only_when_valid(store):
    store.save_buffer_key("test-buffer-key")
    with_id = {**CHANNEL, "x_account_id": "1234567890123456789"}
    store.save_buffer_channels([with_id])
    assert store.buffer_channel(CHANNEL["account_id"])["x_account_id"] == "1234567890123456789"
    for invalid in ("0", "000123", "-1", "18446744073709551616", 123, None):
        with pytest.raises(ValueError, match="Invalid X account ID"):
            store.save_buffer_channels([{**CHANNEL, "x_account_id": invalid}])


def test_direct_fallback_setting_defaults_off_and_requires_boolean(store):
    assert store.buffer_direct_fallback_enabled() is False
    store.set_buffer_direct_fallback_enabled(True)
    assert store.buffer_direct_fallback_enabled() is True
    with pytest.raises(ValueError, match="Boolean"):
        store.set_buffer_direct_fallback_enabled("true")
    store.set_buffer_direct_fallback_enabled(False)
    assert store.buffer_direct_fallback_enabled() is False


async def test_admin_sync_keeps_only_verified_x_identity(monkeypatch):
    import x_publisher.buffer_api as buffer_module

    class FakeBufferAPI:
        def __init__(self, key):
            assert key == "test-buffer-key"

        async def list_channels(self):
            return [
                {"id": "channel_123", "service": "twitter", "name": "My account",
                 "username": "example", "x_account_id": "1234567890123456789",
                 "isDisconnected": False, "isLocked": False},
                {"id": "channel_456", "service": "twitter", "name": "Other",
                 "username": "other", "isDisconnected": False, "isLocked": False},
            ]

        async def close(self):
            pass

    monkeypatch.setattr(buffer_module, "BufferAPI", FakeBufferAPI)
    channels = await verified_buffer_channels("test-buffer-key")
    assert channels[0]["x_account_id"] == "1234567890123456789"
    assert "x_account_id" not in channels[1]


async def test_buffer_oauth_consent_selects_only_buffer_channels(store):
    store.save_buffer_key("test-buffer-key")
    store.save_buffer_channels([CHANNEL])
    callback = "https://chatgpt.com/connector/oauth/buffer-test"
    store.set_setting("callbacks", [callback])
    app = create_app(store, "owner-" + "x" * 60, Backend)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN,
            follow_redirects=False) as client:
        metadata = (await client.get("/.well-known/oauth-authorization-server/x-mcp/oauth")).json()
        registration = await client.post(metadata["registration_endpoint"], json={
            "redirect_uris": [callback], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
        assert registration.status_code == 201
        params = {"client_id": registration.json()["client_id"], "redirect_uri": callback,
                  "response_type": "code", "code_challenge": "v" * 43,
                  "code_challenge_method": "S256", "scope": "buffer:status buffer:publish",
                  "resource": RESOURCE, "state": "bufferstate"}

        async def consent_request():
            start = await client.get(metadata["authorization_endpoint"], params=params)
            assert start.status_code in (302, 303, 307)
            url = start.headers["location"]
            return parse_qs(urlsplit(url).query)["request"][0], await client.get(url)

        pending, page = await consent_request()
        assert page.status_code == 200
        assert 'name="accounts" value="buffer:channel_123"' in page.text
        assert 'name="accounts" value="1"' not in page.text
        assert 'name="approve_scope" value="buffer:publish"' in page.text
        rejected = await client.post("/x-mcp/oauth/consent", data={
            "request": pending, "key": "owner-" + "x" * 60,
            "accounts": "1", "approve_scope": "buffer:publish"}, headers={"Origin": ORIGIN})
        assert rejected.status_code == 400
        assert rejected.json()["error"] == "select_active_accounts"

        pending, _ = await consent_request()
        approved = await client.post("/x-mcp/oauth/consent", data={
            "request": pending, "key": "owner-" + "x" * 60,
            "accounts": CHANNEL["account_id"], "approve_scope": "buffer:publish"},
            headers={"Origin": ORIGIN})
        assert approved.status_code == 303
        assert parse_qs(urlsplit(approved.headers["location"]).query)["state"] == ["bufferstate"]
