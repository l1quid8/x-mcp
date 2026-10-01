"""The shared endpoint must keep public reading separate from account writes."""

from datetime import datetime, timezone

import httpx
import pytest

from nitter_mcp.parse import Post
from x_publisher.app import create_app
from test_publisher import Backend, rpc, store


class FakeReadPool:
    def __init__(self):
        self.paths = []
        self.closed = False

    async def fetch(self, path, capability, **kwargs):
        self.paths.append((path, capability))
        post = Post(
            id="123", author="@Example", text="Public example", created_at=datetime.now(timezone.utc).isoformat(),
            age_minutes=1, url="https://x.com/Example/status/123", source_instance="https://public.example",
        )
        return [post], "Example", "https://public.example"

    async def aclose(self):
        self.closed = True


async def test_one_catalog_with_separate_read_and_write_grants(store):
    read_token = store.issue_token("read-only", ["x:read"], [])
    write_token = store.issue_token("publisher", ["publisher:status", "publisher:publish"], ["1"])
    pool = FakeReadPool()
    app = create_app(store, "a" * 64, Backend, read_pool=pool)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://mcp.example.test") as client:
        listed = (await rpc(client, read_token, "tools/list")).json()["result"]["tools"]
        assert len(listed) == 18
        assert {"search_x", "reader_status", "publish_publication", "execute_deletion_plan"} <= {
            tool["name"] for tool in listed}

        read = (await rpc(client, read_token, "tools/call", {
            "name": "search_x", "arguments": {"query": "example"}})).json()["result"]
        assert not read.get("isError"), read
        assert read["structuredContent"]["posts"][0]["id"] == "123"
        assert len(pool.paths) == 1

        denied_write = (await rpc(client, read_token, "tools/call", {
            "name": "preview_publication", "arguments": {"account_id": "1", "content": {
                "kind": "post", "posts": [{"text": "hello"}]}}})).json()["result"]
        assert denied_write["isError"] and "insufficient_scope" in denied_write["content"][0]["text"]
        denied_delete = (await rpc(client, read_token, "tools/call", {
            "name": "execute_deletion_plan", "arguments": {"account_id": "1", "plan_id": "x",
                "idempotency_key": "request-1", "dry_run": False}})).json()["result"]
        assert denied_delete["isError"] and "insufficient_scope" in denied_delete["content"][0]["text"]

        denied_read = (await rpc(client, write_token, "tools/call", {
            "name": "search_x", "arguments": {"query": "example"}})).json()["result"]
        assert denied_read["isError"] and "insufficient_scope" in denied_read["content"][0]["text"]
        assert len(pool.paths) == 1
        status = (await rpc(client, write_token, "tools/call", {
            "name": "publishing_status", "arguments": {}})).json()["result"]
        assert status["structuredContent"]["accounts"][0]["account_id"] == "1"
    assert pool.closed


def test_read_grant_never_carries_an_account(store):
    token = store.issue_token("read", ["x:read"], ["1"])
    assert store.token_principal(token).accounts == frozenset()
