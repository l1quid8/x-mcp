"""Account-scoped Buffer submission with immutable drafts and honest receipts.

Buffer accepting a post is not proof that X published it. Delivery status is
reported separately, and a transport failure during creation is never retried.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import warnings
from datetime import datetime, timezone
from urllib.parse import urlsplit

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message="pkg_resources is deprecated as an API.*", category=UserWarning)
    from twitter_text import parse_tweet
    from twitter_text.config import config as text_config

from .buffer_api import BufferAPI, BufferError
from .core import Attachment, Post, Problem, Publication, TERMINAL, canonical, identifier
from .media import public_url


_MODES = frozenset({"shareNow", "addToQueue", "customScheduled"})


class BufferPublisher:
    def __init__(self, store, api_factory=BufferAPI, direct_publisher=None, media_store=None):
        self.store = store
        self.api_factory = api_factory
        self.direct_publisher = direct_publisher
        self.media_store = media_store
        self.account_locks: dict[str, asyncio.Lock] = {}
        self.tasks: set[asyncio.Task] = set()

    def _channel(self, account_id: str) -> dict:
        if not isinstance(account_id, str) or not account_id.startswith("buffer:"):
            raise Problem("buffer_account_required", "Choose a connected Buffer X channel")
        channel = self.store.buffer_channel(account_id)
        if not channel or channel.get("channel_id") != account_id[len("buffer:"):]:
            raise Problem("buffer_account_unavailable", "Reconnect this Buffer X channel")
        if not self.store.buffer_key():
            raise Problem("buffer_not_configured", "Configure a Buffer API key before publishing")
        return channel

    def _connection_key(self, payload: dict, channel: dict) -> str:
        """Use only the verified Buffer identity and exact key frozen at preview."""
        key = self.store.buffer_key()
        if (not key or payload.get("_buffer_key_fingerprint") != hashlib.sha256(key.encode()).hexdigest()
                or payload.get("x_account_id") != channel.get("x_account_id")):
            raise Problem("buffer_connection_changed", "Buffer connection changed; preview this post again")
        return key

    @staticmethod
    def _content(text: str, image_urls: list[str] | None, mode: str, due_at: str | None) -> dict:
        if not isinstance(text, str):
            raise Problem("invalid_text", "Post text must be a string")
        if not isinstance(image_urls, list) or any(not isinstance(url, str) for url in image_urls):
            raise Problem("invalid_media_url", "Provide image URLs as a list")
        if len(image_urls) > 4:
            raise Problem("too_many_images", "Use no more than four images")
        if not text.strip() and not image_urls:
            raise Problem("empty_post", "Provide text or at least one image")
        parsed = parse_tweet(text, {**text_config["defaults"], "max_weighted_tweet_length": 280})
        if parsed.weightedLength > 280:
            raise Problem("text_too_long", "Text exceeds X's 280-character weighted limit")
        if text and not parsed.valid:
            raise Problem("invalid_text", "Text contains characters that X does not permit")
        for url in image_urls:
            if len(url) > 4096:
                raise Problem("invalid_media_url", "An image URL is too long")
            try:
                public_url(url)
                host = urlsplit(url).hostname
                if not host or host.lower().endswith((".internal", ".test", ".invalid")):
                    raise Problem("invalid_media_url", "Images need publicly accessible HTTPS URLs")
            except (Problem, ValueError) as exc:
                raise Problem("invalid_media_url", "Images need publicly accessible HTTPS URLs") from exc
        if not isinstance(mode, str) or mode not in _MODES:
            raise Problem("invalid_delivery_mode", "Choose shareNow, addToQueue, or customScheduled")
        if mode == "customScheduled":
            if not isinstance(due_at, str):
                raise Problem("invalid_schedule", "Provide a future date and time with a time zone")
            try:
                when = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise Problem("invalid_schedule", "Provide a future date and time with a time zone") from exc
            if when.tzinfo is None or when.utcoffset() is None or when.timestamp() <= time.time():
                raise Problem("invalid_schedule", "Provide a future date and time with a time zone")
            due_at = when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        elif due_at is not None:
            raise Problem("invalid_schedule", "Only customScheduled accepts a scheduled time")
        return {"text": text, "image_urls": image_urls, "mode": mode, "due_at": due_at}

    def preview(self, account_id: str, text: str, image_urls: list[str] | None = None,
                mode: str = "shareNow", due_at: str | None = None) -> dict:
        channel = self._channel(account_id)
        content = self._content(text, image_urls if image_urls is not None else [], mode, due_at)
        key = self.store.buffer_key()
        payload = canonical({"provider": "buffer", "channel_id": channel["channel_id"],
                             "x_account_id": channel.get("x_account_id"),
                             "_buffer_key_fingerprint": hashlib.sha256(key.encode()).hexdigest(), **content})
        digest = hashlib.sha256((account_id + "\n" + payload).encode()).hexdigest()
        draft_id = identifier()
        with self.store.db:
            self.store.db.execute("INSERT INTO drafts VALUES (?,?,?,?,?)", (
                draft_id, account_id, payload, digest, time.time() + 86400))
        return {"draft_id": draft_id, "account_id": account_id,
                "channel": {"display_name": channel.get("display_name"), "handle": channel.get("handle")},
                "content": {"provider": "buffer", "channel_id": channel["channel_id"], **content},
                "publishable": True, "blockers": [],
                "expires_in_seconds": 86400, "published": False}

    def _fallback_account(self, channel: dict, account_id: str) -> None:
        if (not isinstance(account_id, str) or not account_id.isdecimal()
                or channel.get("x_account_id") != account_id):
            raise Problem("fallback_account_mismatch", "The Buffer channel is not linked to this direct X account")
        if self.direct_publisher is None or self.media_store is None:
            raise Problem("fallback_unavailable", "Direct X publishing is not configured")
        self.store.account(account_id)

    def submit(self, account_id: str, draft_id: str, idempotency_key: str,
               fallback_account_id: str | None = None) -> dict:
        if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 128:
            raise Problem("invalid_idempotency_key", "Use a stable request key of 8–128 characters")
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            old = self.store.db.execute(
                "SELECT * FROM operations WHERE account=? AND idempotency=?",
                (account_id, idempotency_key)).fetchone()
            draft = self.store.db.execute(
                "SELECT * FROM drafts WHERE id=? AND account=?",
                (draft_id, account_id)).fetchone()
            if old:
                previous = json.loads(old["result"])
                if previous.get("provider") != "buffer" or previous.get("draft_id") != draft_id:
                    raise Problem("idempotency_conflict", "This request key already belongs to a different draft")
                if draft and old["hash"] != draft["hash"]:
                    raise Problem("idempotency_conflict", "This request key already belongs to different content")
                return self.status(old["id"])
            channel = self._channel(account_id)
            if not draft or draft["expires"] <= time.time():
                raise Problem("draft_unavailable", "Draft is missing, expired, or belongs to another account")
            payload = json.loads(draft["payload"])
            if payload.get("provider") != "buffer" or payload.get("channel_id") != channel["channel_id"]:
                raise Problem("draft_unavailable", "This draft does not belong to the selected Buffer channel")
            self._connection_key(payload, channel)
            self._content(payload.get("text"), payload.get("image_urls"), payload.get("mode"), payload.get("due_at"))
            if fallback_account_id is not None:
                if payload["mode"] != "shareNow":
                    raise Problem("fallback_unsupported_mode", "Automatic direct X fallback supports only shareNow posts")
                self._fallback_account(channel, fallback_account_id)
            operation_id = identifier()
            now = time.time()
            result = {"provider": "buffer", "draft_id": draft_id, "buffer_post": None,
                      "buffer_status": None, "delivery_status": "not_submitted",
                      "buffer_reports_sent": False, "x_verified": False, "error": None,
                      "fallback_account_id": fallback_account_id, "fallback_used": False,
                      "fallback_reason": None, "provider_used": None,
                      "direct_operation_id": None, "direct_receipt": None}
            self.store.db.execute("INSERT INTO operations VALUES (?,?,?,?,?,'queued',?,'queued',?,?)", (
                operation_id, account_id, idempotency_key, draft["hash"], draft["payload"],
                canonical(result), now, now))
        task = asyncio.create_task(self.run(operation_id))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return self.status(operation_id)

    def status(self, operation_id: str) -> dict:
        row = self.store.operation(operation_id)
        result = json.loads(row["result"])
        if result.get("provider") != "buffer":
            raise Problem("operation_not_found", "Unknown Buffer operation")
        return {"operation_id": operation_id, "account_id": row["account"],
                "state": row["state"], "phase": row["phase"], **result}

    @staticmethod
    def _record_delivery(result: dict, post: dict) -> str:
        status = post["status"]
        result["buffer_post"] = {"id": post["id"], "channel_id": post["channelId"],
                                 "due_at": post.get("dueAt"), "sent_at": post.get("sentAt")}
        result["buffer_status"] = status
        result["buffer_reports_sent"] = status == "sent"
        result["x_verified"] = False  # Buffer's status is not an X read-back.
        result["provider_used"] = "buffer"
        result.pop("refresh_error", None)
        if status == "sent":
            result["delivery_status"] = "sent_reported_by_buffer"
            return "buffer_reports_sent"
        if status == "error":
            result["delivery_status"] = "failed_reported_by_buffer"
            result["error"] = {"code": "buffer_delivery_failed",
                               "message": "Buffer reports this post failed; inspect it in Buffer"}
            return "buffer_delivery_failed"
        result["error"] = None
        if status == "needs_approval":
            result["delivery_status"] = "awaiting_approval_in_buffer"
        elif status == "sending":
            result["delivery_status"] = "sending_in_buffer"
        else:
            result["delivery_status"] = "accepted_by_buffer"
        return "buffer_accepted"

    def _record_direct(self, operation_id: str, result: dict, child: dict) -> None:
        """Project the direct child receipt without claiming Buffer delivered it."""
        # Serialize projections across status requests and workers. Read the
        # child's newest durable state while holding the SQLite write lock so
        # an older observation cannot overwrite a verified parent receipt.
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            current = json.loads(self.store.operation(operation_id)["result"])
            child = self.direct_publisher.status(child["operation_id"])
            result.clear()
            result.update(current)
            result["direct_operation_id"] = child["operation_id"]
            result["direct_receipt"] = child
            result["provider_used"] = "direct_x"
            result["fallback_used"] = True
            result["x_verified"] = child["state"] == "succeeded" and bool(child.get("posts")) and all(
                post.get("verified") is True for post in child["posts"])
            state = child["state"]
            if state == "succeeded":
                result["delivery_status"] = "verified_on_x" if result["x_verified"] else "direct_submission_complete"
                result["error"] = None
            elif state in {"failed", "partial", "unknown"}:
                result["delivery_status"] = ("direct_outcome_unknown" if state in {"partial", "unknown"}
                                             else "direct_publish_failed")
                result["error"] = child.get("error") or {
                    "code": "direct_publish_failed", "message": "Direct X publishing did not complete"}
            else:
                result["delivery_status"] = "submitting_through_direct_x"
                result["error"] = None
            self.store.db.execute("UPDATE operations SET state=?,result=?,phase=?,updated=? WHERE id=?", (
                state, canonical(result), "direct_" + child.get("phase", state), time.time(), operation_id))

    def _existing_direct_child(self, result: dict) -> dict | None:
        """Find a committed child if the process stopped before linking its ID."""
        operation_id = result.get("direct_operation_id")
        if operation_id:
            return self.direct_publisher.status(operation_id)
        account_id = result.get("fallback_account_id")
        request_key = result.get("direct_request_key")
        if not account_id or not request_key:
            return None
        row = self.store.db.execute(
            "SELECT id FROM operations WHERE account=? AND idempotency=?",
            (account_id, request_key)).fetchone()
        return self.direct_publisher.status(row["id"]) if row else None

    async def _fallback(self, operation_id: str, row: dict, result: dict, payload: dict) -> None:
        if self.store.setting("buffer_direct_fallback_enabled", False) is not True:
            raise Problem("fallback_disabled", "Direct X fallback was disabled")
        account_id = result["fallback_account_id"]
        channel = self._channel(row["account"])
        self._connection_key(payload, channel)
        self._fallback_account(channel, account_id)
        if payload["mode"] != "shareNow":
            raise Problem("fallback_unsupported_mode", "Automatic direct X fallback supports only shareNow posts")
        result["fallback_reason"] = "buffer_api_rate_limited"
        result["delivery_status"] = "preparing_direct_x"
        self.store.update_operation(operation_id, "running", result, "preparing_direct_x")
        attachments = []
        for number, url in enumerate(payload["image_urls"], start=1):
            media = await self.media_store.fetch(account_id, url, f"buffer-image-{number}",
                                                max_bytes=5 * 1024**2)
            if media.get("kind") != "image":
                raise Problem("unsupported_media", "Direct X fallback accepts still images only")
            attachments.append(Attachment(media_id=media["media_id"]))
        publication = Publication(kind="post", posts=[Post(text=payload["text"], attachments=attachments)])
        preview = self.direct_publisher.preview(account_id, publication)
        if not preview["publishable"]:
            blocker = preview["blockers"][0]
            raise Problem(blocker["code"], blocker["message"])
        result["direct_draft_id"] = preview["draft_id"]
        result["direct_request_key"] = "buffer-fallback-" + hashlib.sha256(operation_id.encode()).hexdigest()
        self.store.update_operation(operation_id, "running", result, "submitting_to_direct_x")
        if not self.store.buffer_direct_fallback_enabled():
            raise Problem("fallback_disabled", "Direct X fallback was disabled")
        self._connection_key(payload, self._channel(row["account"]))
        child = self.direct_publisher.submit(account_id, preview["draft_id"], result["direct_request_key"])
        self._record_direct(operation_id, result, child)
        # The child publisher owns the X write and its idempotency key. Poll only
        # its local receipt; never reissue the Buffer or direct create call.
        while child["state"] not in TERMINAL:
            await asyncio.sleep(0.25)
            child = self.direct_publisher.status(child["operation_id"])
            self._record_direct(operation_id, result, child)

    async def run(self, operation_id: str) -> None:
        row = self.store.operation(operation_id)
        lock = self.account_locks.setdefault(row["account"], asyncio.Lock())
        async with lock:
            result = json.loads(row["result"])
            api = None
            submitting = False
            try:
                channel = self._channel(row["account"])
                payload = json.loads(row["payload"])
                if payload.get("provider") != "buffer" or payload.get("channel_id") != channel["channel_id"]:
                    raise Problem("draft_unavailable", "This draft does not belong to the selected Buffer channel")
                key = self._connection_key(payload, channel)
                self._content(payload.get("text"), payload.get("image_urls"), payload.get("mode"), payload.get("due_at"))
                api = self.api_factory(key)
                self.store.update_operation(operation_id, "running", result, "submitting_to_buffer")
                submitting = True
                post = await api.create_post(channel["channel_id"], payload["text"],
                                             payload["image_urls"], payload["mode"], payload["due_at"])
                if post["channelId"] != channel["channel_id"]:
                    result["buffer_post"] = {"id": post["id"], "channel_id": post["channelId"],
                                             "due_at": post.get("dueAt"), "sent_at": post.get("sentAt")}
                    raise Problem("buffer_channel_mismatch", "Buffer returned a different channel; inspect Buffer before retrying")
                phase = self._record_delivery(result, post)
                self.store.update_operation(operation_id, "failed" if post["status"] == "error" else "succeeded",
                                            result, phase)
            except asyncio.CancelledError:
                result["error"] = {"code": "interrupted", "message": "Submission was interrupted; check Buffer before retrying"}
                self.store.update_operation(operation_id, "unknown" if submitting else "failed", result, "interrupted")
                raise
            except Exception as exc:
                if (isinstance(exc, BufferError) and exc.code == "rate_limited"
                        and exc.definite and result.get("fallback_account_id")
                        and result.get("buffer_post") is None and submitting):
                    try:
                        submitting = False  # Buffer definitively rejected this create.
                        await self._fallback(operation_id, row, result, payload)
                        return
                    except asyncio.CancelledError:
                        result["error"] = {"code": "interrupted", "message": "Direct X fallback was interrupted; inspect its receipt"}
                        self.store.update_operation(operation_id, "unknown", result, "interrupted")
                        raise
                    except Exception as fallback_exc:
                        child = self._existing_direct_child(result)
                        if child:
                            # A direct child exists. Its recorded state is the
                            # only reliable outcome; never create another one.
                            self._record_direct(operation_id, result, child)
                            return
                        code = fallback_exc.code if isinstance(fallback_exc, Problem) else "fallback_unavailable"
                        message = (fallback_exc.message if isinstance(fallback_exc, Problem)
                                   else "Direct X fallback could not be prepared")
                        result["error"] = {"code": code, "message": message}
                        self.store.update_operation(operation_id, "failed", result, "fallback_stopped")
                        return
                if isinstance(exc, BufferError):
                    code, message, definite = exc.code, exc.message, exc.definite
                elif isinstance(exc, Problem):
                    code, message, definite = exc.code, exc.message, not submitting
                else:
                    code, message, definite = "buffer_unavailable", "Buffer's response could not be confirmed", False
                result["error"] = {"code": code, "message": message}
                self.store.update_operation(operation_id, "unknown" if submitting and not definite else "failed",
                                            result, "stopped")
            finally:
                if api:
                    try:
                        await api.close()
                    except Exception:
                        pass

    async def refresh(self, operation_id: str) -> dict:
        row = self.store.operation(operation_id)
        result = json.loads(row["result"])
        if result.get("provider") != "buffer":
            raise Problem("operation_not_found", "Unknown Buffer operation")
        child = self._existing_direct_child(result)
        if child:
            self._record_direct(operation_id, result, child)
            return self.status(operation_id)
        post_id = (result.get("buffer_post") or {}).get("id")
        if not post_id:
            return self.status(operation_id)
        lock = self.account_locks.setdefault(row["account"], asyncio.Lock())
        async with lock:
            row = self.store.operation(operation_id)
            result = json.loads(row["result"])
            api = None
            try:
                channel = self._channel(row["account"])
                api = self.api_factory(self.store.buffer_key())
                post = await api.get_post(post_id)
                if post["id"] != post_id or post["channelId"] != channel["channel_id"]:
                    raise Problem("buffer_channel_mismatch", "Buffer returned a different channel; inspect Buffer directly")
                phase = self._record_delivery(result, post)
                self.store.update_operation(operation_id, "failed" if post["status"] == "error" else "succeeded",
                                            result, phase)
            except (BufferError, Problem) as exc:
                result["refresh_error"] = {"code": exc.code, "message": exc.message}
                self.store.update_operation(operation_id, row["state"], result, row["phase"])
            finally:
                if api:
                    try:
                        await api.close()
                    except Exception:
                        pass
        return self.status(operation_id)

    async def close(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
