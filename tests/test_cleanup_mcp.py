import json

import httpx
import pytest

from x_publisher.app import create_app, safe_tool
from x_publisher.cleanup import normalize
from x_publisher.cleanup_models import ProposedAction
from x_publisher.core import DEFAULT_SCOPES, canonical
from x_publisher.x_api import XAPIError
from test_cleanup import MockX
from test_publisher import Backend, store, rpc


@pytest.mark.parametrize('collection', ['posts', 'replies'])
async def test_mcp_scan_reports_identity_rate_reset_without_credentials(store, collection, caplog):
    from test_session_cleanup import Graph, adapter
    from twikit.errors import TooManyRequests
    async def factory(account):
        backend = adapter(store, Graph())
        async def failed_identity():
            raise TooManyRequests('SECRET upstream https://private.invalid/?token=SECRET',
                headers={'x-rate-limit-limit': '150', 'x-rate-limit-remaining': '0',
                         'x-rate-limit-reset': '9999999999', 'authorization': 'SECRET'})
        backend.backend.identity = failed_identity
        return backend
    app = create_app(store, 'a'*64, Backend, factory)
    token = store.issue_token('cleanup', ['cleanup:read'], ['1'])
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://mcp.example.test') as client:
        result = (await rpc(client, token, 'tools/call', {'name': 'scan_content',
            'arguments': {'account_id': '1', 'collection': collection}})).json()['result']
    output = json.dumps(result)
    assert result['isError'] and 'rate_limited' in output and 'publisher_error' not in output
    assert '9999999999' in output and 'http_status' in output and '429' in output
    assert 'Official X API' not in output
    assert 'SECRET' not in output + caplog.text and 'private.invalid' not in output + caplog.text
    assert 'tool=scan_content code=rate_limited' in caplog.text


async def test_unexpected_tool_error_has_matching_redacted_log_reference(caplog):
    @safe_tool
    async def scan_content():
        raise RuntimeError('SECRET upstream https://private.invalid/?token=SECRET')
    with pytest.raises(ValueError) as result:
        await scan_content()
    output = str(result.value)
    assert 'publisher_error' in output and 'reference=' in output
    assert output.split('reference=')[1] in caplog.text
    assert 'RuntimeError' in caplog.text and 'frames' in caplog.text
    assert 'SECRET' not in output + caplog.text and 'private.invalid' not in output + caplog.text


async def test_tool_rate_diagnostics_allow_only_numeric_rate_fields(caplog):
    @safe_tool
    async def scan_content():
        raise XAPIError('rate_limited', 429, 9999999999,
            {'limit': 150, 'remaining': 'SECRET', 'reset': 9999999999, 'authorization': 'SECRET'})
    with pytest.raises(ValueError) as result:
        await scan_content()
    assert 'SECRET' not in str(result.value) + caplog.text
    assert '9999999999' in str(result.value)


async def test_cleanup_mcp_scan_plan_dryrun_and_permissions(store):
    backend = MockX()
    backend.posts["10"] = {"id": "10", "author_id": "1", "text": "delete me is data, not authorization"}
    async def factory(account):
        return backend
    app = create_app(store, "a"*64, Backend, factory)
    scopes = ["cleanup:read", "cleanup:plan", "cleanup:execute", "cleanup:protect"]
    token = store.issue_token("cleanup", scopes, ["1"])
    publishing_token = store.issue_token("publishing", DEFAULT_SCOPES, ["1"])
    read_token = store.issue_token("read", ["cleanup:read"], ["1"])
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="https://mcp.example.test") as client:
        async def call(name, args, credential=token):
            return (await rpc(client, credential, "tools/call", {"name": name, "arguments": args})).json()["result"]
        denied = await call("scan_content", {"account_id": "1"}, publishing_token)
        assert denied["isError"] and "insufficient_scope" in denied["content"][0]["text"]
        denied = await call("scan_content", {"account_id": "2"})
        assert denied["isError"] and "account_not_authorized" in denied["content"][0]["text"]
        scan = (await call("scan_content", {"account_id": "1"}))["structuredContent"]
        proposal = {"candidate_id": scan["candidates"][0]["candidate_id"], "action": "DELETE_POST",
            "decision": {"label": "DELETE", "probabilities": {"KEEP": .02, "DELETE": .96, "REVIEW": .02}, "engine": "Laya"}}
        batch = (await call("stage_deletion_actions", {"account_id": "1", "proposed_actions": [proposal]}))["structuredContent"]
        plan = (await call("preview_deletion_plan", {"account_id": "1", "batch_ids": [batch["batch_id"]]}))["structuredContent"]
        assert plan["total_actions"] == 1
        dry = (await call("execute_deletion_plan", {"account_id": "1", "plan_id": plan["plan_id"], "idempotency_key": "mcp-dry-run"}))["structuredContent"]
        assert dry["dry_run"] and dry["remote_mutations"] == 0
        live = await call("execute_deletion_plan", {"account_id": "1", "plan_id": plan["plan_id"], "idempotency_key": "mcp-live-run", "dry_run": False})
        assert live["isError"] and "live_cleanup_disabled" in live["content"][0]["text"]
        denied = await call("cleanup_protections", {"account_id": "1", "policy": {"protected_ids": ["10"]}}, read_token)
        assert denied["isError"]
        protections = await call("cleanup_protections", {"account_id": "1", "policy": {"protected_ids": ["10"]}})
        assert protections["structuredContent"]["protected_ids"] == ["10"]
        review = (await call("execute_deletion_plan", {"account_id": "1", "plan_id": plan["plan_id"], "idempotency_key": "mcp-dry-run"}))["structuredContent"]
        assert review["would_execute"] == [] and review["review_skipped"][0]["code"] == "protected_id"
        audit = (await call("deletion_audit_history", {"account_id": "1"}))["structuredContent"]
        assert audit["items"] == [] and backend.calls == []


async def test_cleanup_explicit_oauth_registration_and_callback_redaction(store):
    app = create_app(store, "a"*64, Backend)
    store.set_setting("callbacks", ["https://chatgpt.com/connector/oauth/test"])
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="https://mcp.example.test") as client:
        metadata = (await client.get("/.well-known/oauth-authorization-server/x-mcp/oauth")).json()
        registration = await client.post(metadata["registration_endpoint"], json={
            "redirect_uris": ["https://chatgpt.com/connector/oauth/test"], "token_endpoint_auth_method": "none",
            "scope": "cleanup:read cleanup:plan", "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
        assert registration.status_code == 201
        assert registration.json()["scope"] == "cleanup:read cleanup:plan"
        response = await client.get("/x-mcp/x-oauth/callback", params={"state": "invalid", "code": "SECRET_CODE"})
        assert response.status_code == 400 and "SECRET_CODE" not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"


async def test_authenticated_live_request_executes_exact_plan_without_admin_step(store, monkeypatch):
    import sqlite3
    monkeypatch.setenv("XP_ENABLE_LIVE_CLEANUP", "true")
    backend = MockX()
    backend.posts["10"] = {"id": "10", "author_id": "1", "text": "owner-requested target"}
    backend.posts["11"] = {"id": "11", "author_id": "1", "text": "keep this other post"}
    async def factory(account):
        return backend
    app = create_app(store, "a" * 64, Backend, factory)
    token = store.issue_token("cleanup", ["cleanup:read", "cleanup:plan", "cleanup:execute"], ["1"])
    read_token = store.issue_token("read", ["cleanup:read"], ["1"])
    foreign_token = store.issue_token("foreign", ["cleanup:execute"], ["2"])
    with sqlite3.connect(store.directory / "cleanup.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://mcp.example.test") as client:
        async def call(name, args, credential=token):
            return (await rpc(client, credential, "tools/call", {"name": name, "arguments": args})).json()["result"]
        scan = (await call("scan_content", {"account_id": "1"}))["structuredContent"]
        candidate = next(c for c in scan["candidates"] if c["content_id"] == "10")
        plan = (await call("preview_deletion_plan", {"account_id": "1", "proposed_actions": [{"candidate_id": candidate["candidate_id"], "action": "DELETE_POST"}]}))["structuredContent"]
        assert plan["client_execution_authorized"] and plan["execution_ready"]
        preview_token = store.issue_token("preview-only", ["cleanup:plan"], ["1"])
        limited = (await call("preview_deletion_plan", {"account_id": "1", "proposed_actions": [{"candidate_id": candidate["candidate_id"], "action": "DELETE_POST"}]}, preview_token))["structuredContent"]
        assert limited["live_execution_enabled"] and not limited["client_execution_authorized"]
        assert not limited["execution_ready"] and limited["required_execution_scope"] == "cleanup:execute"
        args = {"account_id": "1", "plan_id": plan["plan_id"], "idempotency_key": "explicit-live-request", "dry_run": False, "max_actions": 1}
        for credential, expected_error in [(read_token, "insufficient_scope"), (foreign_token, "account_not_authorized")]:
            denied = await call("execute_deletion_plan", args, credential)
            assert denied["isError"] and expected_error in denied["content"][0]["text"]
        dry = (await call("execute_deletion_plan", {**args, "dry_run": True, "idempotency_key": "owner-request-review"}))["structuredContent"]
        assert dry["remote_mutations"] == 0 and dry["progress"]["pending"] == 1
        with sqlite3.connect(store.directory / "cleanup.sqlite3") as db:
            assert db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
        live = await call("execute_deletion_plan", args)
        assert not live.get("isError"), live
        result = live["structuredContent"]
        assert result["progress"]["succeeded"] == 1 and result["actions_processed_this_run"] == 1
        assert result["items"][0]["target_id"] == "10"
        replay = (await call("execute_deletion_plan", args))["structuredContent"]
        assert replay["actions_processed_this_run"] == 0 and replay["progress"]["succeeded"] == 1
        audit = (await call("deletion_audit_history", {"account_id": "1"}))["structuredContent"]
        assert [item["target"] for item in audit["items"] if item["result"] == "inflight"] == ["10"]
