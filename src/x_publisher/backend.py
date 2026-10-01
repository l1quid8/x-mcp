"""All session-authenticated X operations live behind this adapter."""
import asyncio
import io
import json
import re
import time

import httpx
from markdown_it import MarkdownIt
from twikit import Client

from .core import Problem
from .transaction import CurrentTransaction
from .x_api import rate_headers


def classify(exc):
    name = type(exc).__name__
    if name in {"Unauthorized", "Forbidden", "AccountLocked", "AccountSuspended"}:
        return "session_or_account_restricted", "Reconnect the account or resolve the restriction in X"
    if name == "TooManyRequests":
        return "rate_limited", "X rate-limited this account; do not retry until its limit resets"
    if name in {"DuplicateTweet", "InvalidMedia", "CouldNotTweet", "BadRequest", "NotFound"}:
        return "x_rejected", f"X rejected this request ({name})"
    return "backend_error", f"X backend failed ({name}); details and credentials are not exposed"


def article_blocks(markdown):
    """Lossless source retained beside parsed structure for the Article protocol adapter."""
    tokens = MarkdownIt("commonmark", {"html": False}).enable("strikethrough").parse(markdown)
    return [t.as_dict() for t in tokens]


class XBackend:
    articles_ready = False

    def __init__(self, cookies):
        self.client = Client("en-US", timeout=httpx.Timeout(60, connect=20))
        self.client.set_cookies(cookies)
        self.client.client_transaction = CurrentTransaction()
        self.verification_stage = "not_started"

    async def close(self):
        await self.client.http.aclose()

    def session_snapshot(self):
        return self.client.get_cookies()

    async def identity(self):
        # Settings identifies the authenticated session. Never infer ownership
        # from supplied cookies, the selected handle, or a public user lookup.
        self.verification_stage = "authenticated_settings"
        self.identity_rates = {}
        settings, settings_response = await self.client.v11.settings()
        self.identity_rates["authenticated_settings"] = rate_headers(getattr(settings_response, "headers", {}))
        username = settings.get("screen_name") if isinstance(settings, dict) else None
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", username):
            raise Problem("backend_error", "X did not return an authenticated account identity")
        self.verification_stage = "identity_lookup"
        response, profile_response = await self.client.gql.user_by_screen_name(username)
        self.identity_rates["user_by_screen_name"] = rate_headers(getattr(profile_response, "headers", {}))
        # Avoid Twikit's full User constructor: optional biography and image
        # fields are unrelated to session verification and can be absent.
        try:
            user = response["data"]["user"]["result"]
            account_id = user["rest_id"]
            resolved = (user.get("core") or {}).get("screen_name") or (user.get("legacy") or {}).get("screen_name")
        except (KeyError, TypeError, AttributeError):
            raise Problem("backend_error", "X returned an unsupported account response") from None
        if (user.get("__typename") != "User" or not isinstance(account_id, str)
                or not re.fullmatch(r"[0-9]+", account_id)
                or not isinstance(resolved, str) or resolved.lower() != username.lower()):
            raise Problem("backend_error", "X returned inconsistent account identity data")
        self.client._user_id = account_id
        self.verification_stage = "identity_verified"
        return {"id": account_id, "username": resolved}

    async def upload(self, path, metadata, long_video=False):
        """Sequential disk-backed chunks replace Twikit's eager whole-file uploader."""
        kind = metadata["kind"]
        category = {"image": "tweet_image", "gif": "tweet_gif", "video": "tweet_video"}[kind]
        size = path.stat().st_size
        response, _ = await self.client.v11.upload_media_init(metadata["mime"], size, category, long_video)
        media_id = response.get("media_id_string") or str(response["media_id"])
        with path.open("rb") as f:
            index = 0
            while chunk := f.read(4 * 1024 * 1024):
                # Retry is deliberately left to the caller, never hidden in the adapter.
                with io.BytesIO(chunk) as part:
                    await self.client.v11.upload_media_append(long_video, media_id, index, part)
                index += 1
        response, _ = await self.client.v11.upload_media_finelize(long_video, media_id)
        if kind != "image":
            deadline = time.monotonic() + 1800
            while True:
                info = response.get("processing_info", {})
                if "error" in info or info.get("state") == "failed":
                    raise Problem("media_processing_failed", "X failed to process the uploaded media")
                if info.get("state") == "succeeded":
                    break
                if time.monotonic() >= deadline:
                    raise Problem("media_processing_timeout", "X media processing did not finish in time")
                await asyncio.sleep(min(max(info.get("check_after_secs", 2), 1), 30))
                response, _ = await self.client.v11.upload_media_status(long_video, media_id)
        return media_id

    async def metadata(self, media_id, alt_text):
        if alt_text:
            await self.client.create_media_metadata(media_id, alt_text=alt_text)

    async def poll(self, choices, duration_minutes):
        return await self.client.create_poll(choices, duration_minutes)

    async def create_post(self, post, media_ids, poll_uri, reply_to):
        # Read the receipt directly: constructing Twikit's full Tweet/User after
        # submission can discard a valid receipt when optional metadata is absent.
        response, _ = await self.client.gql.create_tweet(
            is_note_tweet=post["long_post"], text=post["text"],
            media_entities=[{"media_id": mid, "tagged_users": []} for mid in media_ids],
            poll_uri=poll_uri, reply_to=reply_to,
            attachment_url=(f"https://x.com/i/status/{post['quote_id']}" if post.get("quote_id") else None),
            community_id=None, share_with_followers=False, richtext_options=None,
            edit_tweet_id=None, limit_mode=None)
        branch = "notetweet_create" if post["long_post"] else "create_tweet"
        try:
            tweet = response["data"][branch]["tweet_results"]["result"]
            if tweet.get("__typename") == "TweetWithVisibilityResults":
                tweet = tweet["tweet"]
            post_id = tweet["rest_id"]
        except (KeyError, TypeError, AttributeError):
            # Preserve upstream's explicit rejection classifications, but never
            # retry an unrecognized response after sending a write.
            if isinstance(response, dict) and response.get("errors"):
                from twikit.errors import raise_exceptions_from_response
                raise_exceptions_from_response(response["errors"])
            raise Problem("unknown_outcome", "X did not return a valid published post ID") from None
        if not isinstance(post_id, str) or not re.fullmatch(r"[0-9]+", post_id):
            raise Problem("unknown_outcome", "X did not return a valid published post ID")
        return {"id": post_id, "url": f"https://x.com/i/status/{post_id}"}

    async def verify_post(self, post_id, account_id):
        response, _ = await self.client.gql.tweet_detail(post_id, None)
        def matches(value):
            if isinstance(value, dict):
                if value.get("__typename") == "Tweet" and value.get("rest_id") == post_id:
                    author = value.get("core", {}).get("user_results", {}).get("result", {}).get("rest_id")
                    legacy_author = value.get("legacy", {}).get("user_id_str")
                    return (author == account_id or legacy_author == account_id) and all(
                        v in (None, account_id) for v in (author, legacy_author))
                return any(matches(v) for v in value.values())
            return isinstance(value, list) and any(matches(v) for v in value)
        return matches(response)

    async def create_article(self, article, checkpoint):
        # No guessed GraphQL mutation is sent. Protocol fixtures from the owner's
        # authorized Article editor are required before enabling this operation.
        raise Problem("article_backend_unverified", "Article publishing needs authenticated protocol validation; no Article was sent")


def capability_defaults(tier="unknown"):
    premium = tier in {"basic", "premium", "premium_plus"}
    return {
        "tier": tier, "source": "owner_attested" if tier != "unknown" else "unverified",
        "text_limit": 25000 if premium else 280,
        "long_posts": premium if tier != "unknown" else None,
        "long_video": premium if tier != "unknown" else None,
        "articles": tier in {"premium", "premium_plus"} if tier != "unknown" else None,
        "video_max_bytes": 8 * 1024**3 if premium else 512 * 1024**2,
        "video_max_seconds": 10800 if premium else 140,
        "limits_verified_live": False,
    }
