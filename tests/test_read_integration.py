"""The merged server must keep public reads scoped, labelled, and bounded."""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nitter_mcp import config  # noqa: E402
from nitter_mcp import server as reader_module  # noqa: E402
from nitter_mcp.parse import Post  # noqa: E402
from nitter_mcp.pool import AllInstancesFailed, NitterPool  # noqa: E402
from nitter_mcp.server import register_read_tools  # noqa: E402


class FakePool:
    def __init__(self, host: str):
        self.host = host
        self.fetches = []
        self.health_checks = 0

    async def fetch(self, path, capability, **kwargs):
        self.fetches.append((path, capability, kwargs))
        await asyncio.sleep(0)
        post = Post(
            id="123", author="@Reuters", text="A public post", created_at="2026-10-01T12:00:00Z",
            age_minutes=1.0, url="https://x.com/Reuters/status/123", source_instance=self.host,
        )
        return [post], "Reuters feed", self.host

    async def ensure_health(self, force=False):
        self.health_checks += 1

    def snapshot(self):
        return {
            "usable_by_capability": {"timeline": 1, "search": 1},
            "instances": [{"instance": self.host}],
        }

    def clear_cache(self):
        pass


class ReadRegistrationTests(unittest.IsolatedAsyncioTestCase):
    def make_server(self, reader, authorize=lambda ctx: None):
        server = MCPServer(name="read-integration-test")
        register_read_tools(server, authorize, reader)
        return server

    async def test_tool_contract_and_scope_metadata(self):
        server = self.make_server(FakePool("one.test"))
        tools = {tool.name: tool for tool in await server.list_tools()}
        self.assertEqual(
            set(tools), {"search_x", "get_user_posts", "get_breaking_news", "reader_status"}
        )
        for tool in tools.values():
            self.assertNotIn("ctx", tool.input_schema.get("properties", {}))
            self.assertTrue(tool.annotations.read_only_hint)
            self.assertEqual(tool.meta["securitySchemes"][0]["scopes"], ["x:read"])
        self.assertEqual(
            set(tools["get_user_posts"].input_schema["properties"]),
            {"username", "limit", "include_replies", "media_only", "exclude_retweets", "allow_search_fallback"},
        )

    async def test_denial_precedes_every_pool_operation(self):
        reader = FakePool("one.test")

        def deny(ctx):
            raise PermissionError("insufficient_scope")

        server = self.make_server(reader, deny)
        cases = {
            "search_x": {"query": "news"},
            "get_user_posts": {"username": "Reuters"},
            "get_breaking_news": {"sources": ["Reuters"]},
            "reader_status": {},
        }
        for name, arguments in cases.items():
            with self.subTest(name=name), self.assertRaises(ToolError):
                await server.call_tool(name, arguments)
        self.assertEqual(reader.fetches, [])
        self.assertEqual(reader.health_checks, 0)

    async def test_each_app_uses_its_own_pool_and_reports_public_provenance(self):
        one, two = FakePool("one.test"), FakePool("two.test")
        servers = [self.make_server(one), self.make_server(two)]
        results = await asyncio.gather(*(
            server.call_tool("search_x", {"query": "news"}) for server in servers
        ))
        self.assertEqual([r.structured_content["served_by"] for r in results], ["one.test", "two.test"])
        for result in results:
            self.assertEqual(result.structured_content["provenance"]["source"], "public_nitter_rss")
            self.assertEqual(result.structured_content["provenance"]["verification"], "unverified_public_posts")
            self.assertIn("source_instance", result.structured_content["posts"][0])
        self.assertEqual(len(one.fetches), 1)
        self.assertEqual(len(two.fetches), 1)

    async def test_reader_has_no_separate_server_or_pool(self):
        self.assertFalse(any(isinstance(value, MCPServer) for value in vars(reader_module).values()))
        for name in ("server", "pool", "main", "_active_pool"):
            self.assertFalse(hasattr(reader_module, name), name)

        with patch("nitter_mcp.server.NitterPool") as constructor:
            server = self.make_server(FakePool("one.test"))
            result = await server.call_tool("search_x", {"query": "news"})
        self.assertTrue(result.structured_content["ok"])
        constructor.assert_not_called()

    async def test_custom_sources_are_validated_deduplicated_and_capped(self):
        reader = FakePool("one.test")
        server = self.make_server(reader)
        for invalid in (["Reuters/../../other"], ["a" * 16], ["Reuters"] * 26, []):
            with self.subTest(invalid=invalid):
                result = await server.call_tool("get_breaking_news", {"sources": invalid})
                self.assertFalse(result.structured_content["ok"])
                self.assertEqual(result.structured_content["error"], "invalid sources")
        self.assertEqual(reader.fetches, [])
        result = await server.call_tool("get_breaking_news", {"sources": ["@Reuters", "reuters"]})
        self.assertTrue(result.structured_content["ok"])
        self.assertEqual(result.structured_content["sources_used"], ["Reuters"])
        self.assertEqual(len(reader.fetches), 1)


class EmptyCanaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_empty_canary_retires_only_that_capability(self):
        feed = '<?xml version="1.0"?><rss><channel><title>Empty</title></channel></rss>'
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"X_MCP_READER_STATE_DIR": directory}):
            reader = NitterPool(["https://mirror.test"])
            state = reader.instances["https://mirror.test"]
            with patch.object(reader, "_get", new=AsyncMock(return_value=httpx.Response(200, text=feed))), \
                    patch.object(config, "CAP_STRIKES", 2):
                await reader._probe_capability(state, "search", "/search/rss?q=news")
                self.assertIsNot(state.caps.get("search"), False)
                await reader._probe_capability(state, "search", "/search/rss?q=news")
            self.assertIs(state.caps["search"], False)
            self.assertIsNot(state.caps.get("timeline"), False)


class RedirectSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_redirects_cannot_reach_private_or_non_https_targets(self):
        origin = "https://1.1.1.1/Reuters/rss"
        blocked = (
            "https://127.0.0.1/private",
            "https://10.1.2.3/private",
            "https://169.254.169.254/private",
            "http://8.8.8.8/insecure",
        )
        for location in blocked:
            with self.subTest(location=location), tempfile.TemporaryDirectory() as directory, \
                    patch.dict(os.environ, {"X_MCP_READER_STATE_DIR": directory}):
                reader = NitterPool(["https://1.1.1.1"])
                client = AsyncMock()
                client.get.return_value = httpx.Response(302, headers={"Location": location})
                with patch.object(reader, "client", new=AsyncMock(return_value=client)), \
                        patch.object(reader, "_throttle", new=AsyncMock()):
                    with self.assertRaisesRegex(Exception, "public|HTTPS"):
                        await reader._get(reader.instances["https://1.1.1.1"], origin)
                self.assertEqual(client.get.await_count, 1)
                self.assertFalse(client.get.await_args.kwargs["follow_redirects"])

    async def test_configured_private_host_and_private_dns_are_rejected_before_get(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"X_MCP_READER_STATE_DIR": directory}):
            reader = NitterPool(["https://1.1.1.1"])
            client = AsyncMock()
            with patch.object(reader, "client", new=AsyncMock(return_value=client)):
                for url in ("https://localhost/rss", "https://192.168.1.1/rss", "https://[fe80::1]/rss"):
                    with self.subTest(url=url), self.assertRaises(Exception):
                        await reader._get(reader.instances["https://1.1.1.1"], url)
                with patch("nitter_mcp.pool.socket.getaddrinfo", return_value=[
                    (2, 1, 6, "", ("127.0.0.1", 443))
                ]):
                    with self.assertRaisesRegex(Exception, "public internet address"):
                        await reader._get(reader.instances["https://1.1.1.1"], "https://mirror.test/rss")
            client.get.assert_not_awaited()

    async def test_public_https_redirect_is_followed_explicitly(self):
        origin = "https://1.1.1.1/Reuters/rss"
        target = "https://8.8.8.8/Reuters/rss"
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"X_MCP_READER_STATE_DIR": directory}):
            reader = NitterPool(["https://1.1.1.1"])
            client = AsyncMock()
            client.get.side_effect = [
                httpx.Response(302, headers={"Location": target}),
                httpx.Response(200, text="<?xml version='1.0'?><rss><channel/></rss>"),
            ]
            with patch.object(reader, "client", new=AsyncMock(return_value=client)), \
                    patch.object(reader, "_throttle", new=AsyncMock()):
                response = await reader._get(reader.instances["https://1.1.1.1"], origin)
            self.assertEqual(response.status_code, 200)
            self.assertEqual([call.args[0] for call in client.get.await_args_list], [origin, target])
            self.assertTrue(all(call.kwargs["follow_redirects"] is False for call in client.get.await_args_list))

    async def test_rejected_mirror_does_not_echo_userinfo(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"X_MCP_READER_STATE_DIR": directory}):
            reader = NitterPool(["https://operator:secret@1.1.1.1"])
            client = AsyncMock()
            with patch.object(reader, "client", new=AsyncMock(return_value=client)), \
                    patch.object(reader, "ensure_health", new=AsyncMock()):
                with self.assertRaises(AllInstancesFailed) as caught:
                    await reader.fetch("/Reuters/rss", "timeline")
            self.assertNotIn("secret", str(caught.exception))
            self.assertNotIn("operator", str(caught.exception))
            client.get.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
