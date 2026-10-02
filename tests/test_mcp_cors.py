"""Browser access to the MCP transport must keep its bearer boundary intact."""

import httpx
import pytest

from x_publisher.app import create_app
from x_publisher.mcp_origin import CHATGPT_ORIGIN
from test_publisher import Backend, store
from test_unified_app import FakeReadPool


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
async def test_chatgpt_mcp_preflight_allows_transport_methods_without_bearer(store, method):
    app = create_app(store, "a" * 64, Backend, read_pool=FakeReadPool())
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://mcp.example.test") as client:
        response = await client.options("/x-mcp/mcp", headers={
            "Origin": CHATGPT_ORIGIN,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "authorization, content-type, mcp-protocol-version, mcp-session-id, last-event-id",
        })
        assert response.status_code == 204
        assert response.headers["access-control-allow-origin"] == CHATGPT_ORIGIN
        assert method in response.headers["access-control-allow-methods"]
        allowed = {header.strip().lower() for header in response.headers["access-control-allow-headers"].split(",")}
        assert {"authorization", "content-type", "mcp-protocol-version", "mcp-session-id", "last-event-id"} <= allowed
        assert "Access-Control-Request-Method" in response.headers["vary"]


async def test_chatgpt_origin_can_read_challenge_and_authenticated_mcp_response(store):
    token = store.issue_token("chatgpt-cors", ["x:read"], [])
    app = create_app(store, "a" * 64, Backend, read_pool=FakeReadPool())
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://mcp.example.test") as client:
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        denied = await client.post("/x-mcp/mcp", json=request, headers={"Origin": CHATGPT_ORIGIN})
        assert denied.status_code == 401
        assert denied.headers["access-control-allow-origin"] == CHATGPT_ORIGIN
        assert "WWW-Authenticate" in denied.headers["access-control-expose-headers"]
        assert "resource_metadata=" in denied.headers["www-authenticate"]

        granted = await client.post("/x-mcp/mcp", json=request, headers={
            "Origin": CHATGPT_ORIGIN,
            "Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        })
        assert granted.status_code == 200, granted.text
        assert granted.headers["access-control-allow-origin"] == CHATGPT_ORIGIN
        assert "MCP-Session-Id" in granted.headers["access-control-expose-headers"]
        assert "tools" in granted.json()["result"]


async def test_mcp_cors_rejects_untrusted_origins_and_unapproved_preflight_headers(store):
    token = store.issue_token("cors-reject", ["x:read"], [])
    app = create_app(store, "a" * 64, Backend, read_pool=FakeReadPool())
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://mcp.example.test") as client:
        preflight = {"Access-Control-Request-Method": "POST",
                     "Access-Control-Request-Headers": "authorization, content-type"}
        untrusted = await client.options("/x-mcp/mcp", headers={
            **preflight, "Origin": "https://attacker.example"})
        assert untrusted.status_code == 403
        assert "access-control-allow-origin" not in untrusted.headers

        unexpected_header = await client.options("/x-mcp/mcp", headers={
            **preflight, "Origin": CHATGPT_ORIGIN,
            "Access-Control-Request-Headers": "authorization, x-unsafe-header"})
        assert unexpected_header.status_code == 403

        unexpected_method = await client.options("/x-mcp/mcp", headers={
            **preflight, "Origin": CHATGPT_ORIGIN, "Access-Control-Request-Method": "PUT"})
        assert unexpected_method.status_code == 403

        untrusted_post = await client.post("/x-mcp/mcp", json={"jsonrpc": "2.0", "id": 1,
            "method": "tools/list", "params": {}}, headers={
            "Origin": "https://attacker.example", "Authorization": "Bearer " + token})
        assert untrusted_post.status_code == 403
        assert "access-control-allow-origin" not in untrusted_post.headers


async def test_mcp_cors_is_limited_to_transport_path(store):
    app = create_app(store, "a" * 64, Backend, read_pool=FakeReadPool())
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://mcp.example.test") as client:
        callback = await client.get("/x-mcp/x-oauth/callback", headers={"Origin": CHATGPT_ORIGIN})
        assert callback.status_code == 400
        assert "access-control-allow-origin" not in callback.headers

        no_origin = await client.options("/x-mcp/mcp", headers={
            "Access-Control-Request-Method": "POST"})
        assert no_origin.status_code == 401
        assert "access-control-allow-origin" not in no_origin.headers
