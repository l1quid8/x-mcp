"""Account retrieval must survive broken timelines without mislabelling search."""

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nitter_mcp import server
from nitter_mcp.parse import Post
from nitter_mcp.pool import AllInstancesFailed


def post(post_id="1", **overrides):
    data = dict(
        id=post_id, author="@Example_User", text="example",
        created_at="2026-09-27T03:43:49Z", age_minutes=1.0,
        url=f"https://x.com/Example_User/status/{post_id}",
    )
    data.update(overrides)
    return Post(**data)


def failure(capability):
    return AllInstancesFailed([("mirror.test", "HTTP 502")], capability)


class FakeReader:
    def __init__(self, *, return_value=None, side_effect=None):
        self.fetch = AsyncMock(return_value=return_value, side_effect=side_effect)


class UserPostsTests(unittest.IsolatedAsyncioTestCase):
    async def test_healthy_timeline_does_not_call_search(self):
        reader = FakeReader(return_value=([post()], "User feed", "mirror.test"))
        result = await server.get_user_posts(reader, "example_user")
        self.assertTrue(result["ok"])
        self.assertEqual(result["retrieval_method"], "timeline")
        self.assertNotIn("partial", result)
        self.assertEqual(reader.fetch.await_count, 1)

    async def test_failure_falls_back_with_reply_and_order_preserved(self):
        older = post("1", created_at="2026-09-26T00:00:00Z")
        latest = post("2", is_reply=True)
        reader = FakeReader(side_effect=[failure("timeline"), ([older, latest], "Search", "search.test")])
        result = await server.get_user_posts(reader, "@example_user", limit=1, include_replies=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["posts"][0]["id"], "2")
        self.assertEqual(result["retrieval_method"], "search_fallback")
        self.assertTrue(result["partial"])
        self.assertIn("HTTP 502", result["timeline_error"])
        self.assertIn("not guaranteed", result["warning"])
        query = parse_qs(urlsplit(reader.fetch.await_args_list[1].args[0]).query)["q"][0]
        self.assertEqual(query, "from:example_user")

    async def test_search_filters_and_author_are_checked_locally(self):
        candidates = [
            post("other", author="@anotheruser", media=["https://example.test/a.jpg"]),
            post("reply", is_reply=True, media=["https://example.test/a.jpg"]),
            post("repost", is_retweet=True, media=["https://example.test/a.jpg"]),
            post("plain"),
            post("wanted", media=["https://example.test/a.jpg"]),
        ]
        reader = FakeReader(side_effect=[failure("timeline"), (candidates, "Search", "search.test")])
        result = await server.get_user_posts(reader, "example_user", media_only=True, exclude_retweets=True)
        self.assertEqual([p["id"] for p in result["posts"]], ["wanted"])
        query = parse_qs(urlsplit(reader.fetch.await_args_list[1].args[0]).query)["q"][0]
        self.assertEqual(query, "from:example_user -filter:retweets -filter:replies filter:media")

    async def test_both_failures_are_reported(self):
        reader = FakeReader(side_effect=[failure("timeline"), failure("search")])
        result = await server.get_user_posts(reader, "example_user")
        self.assertFalse(result["ok"])
        self.assertIn("timeline", result["timeline_error"])
        self.assertIn("search", result["search_error"])

    async def test_empty_or_unrelated_search_is_not_success(self):
        for items in ([], [post(author="@other")]):
            with self.subTest(items=items):
                reader = FakeReader(side_effect=[failure("timeline"), (items, "Search", "search.test")])
                result = await server.get_user_posts(reader, "example_user")
                self.assertFalse(result["ok"])
                self.assertIn("no matching posts", result["error"])

    async def test_empty_timeline_tries_search(self):
        reader = FakeReader(side_effect=[([], "User", "mirror.test"), ([post()], "Search", "search.test")])
        result = await server.get_user_posts(reader, "example_user")
        self.assertTrue(result["ok"])
        self.assertEqual(result["retrieval_method"], "search_fallback")

    async def test_fallback_can_be_disabled(self):
        reader = FakeReader(side_effect=failure("timeline"))
        result = await server.get_user_posts(reader, "example_user", allow_search_fallback=False)
        self.assertFalse(result["ok"])
        self.assertEqual(reader.fetch.await_count, 1)

    async def test_invalid_handles_never_reach_pool(self):
        reader = FakeReader()
        for handle in ("", "x OR from:other", "a" * 16, "üser", "x/y"):
            self.assertFalse((await server.get_user_posts(reader, handle))["ok"])
        reader.fetch.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
