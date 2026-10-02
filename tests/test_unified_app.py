"""The shared endpoint must keep public reading separate from account writes."""

import asyncio
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
        assert len(listed) == 22
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


async def test_buffer_publish_is_separate_from_direct_x_grants(store):
    store.save_buffer_key("buffer-test-key-123456789")
    store.save_buffer_channels([{"account_id": "buffer:chan-1", "channel_id": "chan-1",
                                 "display_name": "My X account", "handle": "myhandle"}])
    buffer_token = store.issue_token("buffer", ["buffer:status", "buffer:publish"], ["buffer:chan-1"])
    direct_token = store.issue_token("direct", ["publisher:status", "publisher:publish"], ["1"])

    class FakeBufferAPI:
        created = []

        def __init__(self, key):
            assert key == "buffer-test-key-123456789"

        async def create_post(self, channel_id, text, image_urls, mode, due_at):
            self.created.append((channel_id, text, image_urls, mode, due_at))
            return {"id": "post-1", "channelId": channel_id, "text": text,
                    "status": "scheduled", "dueAt": None, "sentAt": None}

        async def get_post(self, post_id):
            assert post_id == "post-1"
            return {"id": post_id, "channelId": "chan-1", "text": "hello",
                    "status": "sent", "dueAt": None, "sentAt": "2026-10-02T12:00:00Z"}

        async def close(self):
            pass

    app = create_app(store, "a" * 64, Backend, read_pool=FakeReadPool(),
                     buffer_api_factory=FakeBufferAPI)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://mcp.example.test") as client:
        denied = (await rpc(client, direct_token, "tools/call", {
            "name": "preview_buffer_post", "arguments": {"account_id": "buffer:chan-1", "text": "hello"}})).json()["result"]
        assert denied["isError"] and "insufficient_scope" in denied["content"][0]["text"]
        denied = (await rpc(client, buffer_token, "tools/call", {
            "name": "preview_publication", "arguments": {"account_id": "1", "content": {
                "kind": "post", "posts": [{"text": "hello"}]}}})).json()["result"]
        assert denied["isError"] and "insufficient_scope" in denied["content"][0]["text"]

        channels = (await rpc(client, buffer_token, "tools/call", {
            "name": "buffer_status", "arguments": {}})).json()["result"]["structuredContent"]
        assert [c["account_id"] for c in channels["channels"]] == ["buffer:chan-1"]
        preview = (await rpc(client, buffer_token, "tools/call", {
            "name": "preview_buffer_post", "arguments": {"account_id": "buffer:chan-1",
                "text": "hello", "image_urls": ["https://images.example.com/photo.jpg"]}})).json()["result"]
        assert not preview.get("isError"), preview
        draft_id = preview["structuredContent"]["draft_id"]
        queued = (await rpc(client, buffer_token, "tools/call", {
            "name": "publish_buffer_post", "arguments": {"account_id": "buffer:chan-1",
                "draft_id": draft_id, "idempotency_key": "buffer-request-1"}})).json()["result"]
        assert not queued.get("isError"), queued
        await asyncio.gather(*app.state.buffer_publisher.tasks)
        operation_id = queued["structuredContent"]["operation_id"]
        receipt = (await rpc(client, buffer_token, "tools/call", {
            "name": "buffer_post_status", "arguments": {"operation_id": operation_id}})).json()["result"]
        assert not receipt.get("isError"), receipt
        assert receipt["structuredContent"]["delivery_status"] == "sent_reported_by_buffer"
        assert receipt["structuredContent"]["x_verified"] is False
        assert len(FakeBufferAPI.created) == 1
