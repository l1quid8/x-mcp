import asyncio
import json
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet
import pytest

from x_publisher.buffer_api import BufferError
from x_publisher.buffer_engine import BufferPublisher
from x_publisher.core import Problem, Store
from x_publisher.engine import Publisher


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path, Fernet.generate_key())
    result.save_buffer_key("test-key")
    result.save_buffer_channels([{"account_id": "buffer:channel123", "channel_id": "channel123",
                                  "display_name": "X account", "handle": "@example"}])
    yield result
    result.db.close()


@pytest.fixture
def api():
    class FakeBufferAPI:
        creates = []
        status = "scheduled"
        failure = None
        get_failure = None

        def __init__(self, key):
            assert key == "test-key"

        async def create_post(self, channel_id, text, image_urls, mode, due_at):
            self.creates.append((channel_id, text, image_urls, mode, due_at))
            await asyncio.sleep(0)
            if self.failure:
                raise self.failure
            return {"id": "post123", "channelId": channel_id, "text": text,
                    "status": self.status, "dueAt": due_at, "sentAt": None}

        async def get_post(self, post_id):
            assert post_id == "post123"
            if self.get_failure:
                raise self.get_failure
            return {"id": post_id, "channelId": "channel123", "text": "hello",
                    "status": self.status, "dueAt": None,
                    "sentAt": "2026-10-02T12:00:00Z" if self.status == "sent" else None}

        async def close(self):
            pass

    return FakeBufferAPI


@pytest.fixture
def engine(store, api):
    return BufferPublisher(store, api)


def test_preview_freezes_text_images_and_schedule(engine, api):
    due_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    draft = engine.preview("buffer:channel123", "hello", ["https://example.com/image.jpg"],
                           "customScheduled", due_at)
    assert draft["publishable"] is True
    assert draft["content"]["provider"] == "buffer"
    assert draft["content"]["image_urls"] == ["https://example.com/image.jpg"]
    assert draft["content"]["due_at"].endswith("Z")
    assert api.creates == []


@pytest.mark.parametrize("text,images,mode,due_at,code", [
    ("", [], "shareNow", None, "empty_post"),
    ("界" * 141, [], "shareNow", None, "text_too_long"),
    ("hello", ["http://example.com/image.jpg"], "shareNow", None, "invalid_media_url"),
    ("hello", ["https://127.0.0.1/image.jpg"], "shareNow", None, "invalid_media_url"),
    ("hello", ["https://example.com/image.jpg"] * 5, "shareNow", None, "too_many_images"),
    ("hello", [], "customScheduled", None, "invalid_schedule"),
    ("hello", [], "addToQueue", "2027-01-01T00:00:00Z", "invalid_schedule"),
    ("hello", [], "unsupported", None, "invalid_delivery_mode"),
])
def test_preview_rejects_invalid_post(engine, text, images, mode, due_at, code):
    with pytest.raises(Problem) as error:
        engine.preview("buffer:channel123", text, images, mode, due_at)
    assert error.value.code == code


def test_provider_qualified_account_required(engine):
    with pytest.raises(Problem) as error:
        engine.preview("channel123", "hello")
    assert error.value.code == "buffer_account_required"


async def test_single_create_idempotency_and_status_refresh(engine, api):
    draft = engine.preview("buffer:channel123", "hello", ["https://example.com/image.jpg"], "addToQueue")
    first = engine.submit("buffer:channel123", draft["draft_id"], "same-request")
    second = engine.submit("buffer:channel123", draft["draft_id"], "same-request")
    assert first["operation_id"] == second["operation_id"]
    await asyncio.gather(*engine.tasks)
    assert len(api.creates) == 1
    assert api.creates[0] == ("channel123", "hello", ["https://example.com/image.jpg"], "addToQueue", None)
    accepted = engine.status(first["operation_id"])
    assert accepted["state"] == "succeeded"
    assert accepted["delivery_status"] == "accepted_by_buffer"
    assert accepted["buffer_reports_sent"] is False
    assert accepted["x_verified"] is False
    api.status = "sent"
    sent = await engine.refresh(first["operation_id"])
    assert sent["delivery_status"] == "sent_reported_by_buffer"
    assert sent["buffer_reports_sent"] is True
    assert sent["x_verified"] is False


async def test_refresh_error_preserves_last_buffer_receipt(engine, api):
    draft = engine.preview("buffer:channel123", "hello")
    operation = engine.submit("buffer:channel123", draft["draft_id"], "same-request")
    await asyncio.gather(*engine.tasks)
    api.get_failure = BufferError("server_error", "Buffer status is unavailable.", definite=False)
    status = await engine.refresh(operation["operation_id"])
    assert status["state"] == "succeeded"
    assert status["delivery_status"] == "accepted_by_buffer"
    assert status["refresh_error"]["code"] == "server_error"
    assert len(api.creates) == 1


async def test_idempotency_key_cannot_publish_different_content(engine, api):
    first = engine.preview("buffer:channel123", "hello")
    second = engine.preview("buffer:channel123", "different")
    engine.submit("buffer:channel123", first["draft_id"], "same-request")
    with pytest.raises(Problem) as error:
        engine.submit("buffer:channel123", second["draft_id"], "same-request")
    assert error.value.code == "idempotency_conflict"
    await asyncio.gather(*engine.tasks)
    assert len(api.creates) == 1


@pytest.mark.parametrize("definite,expected_state", [(False, "unknown"), (True, "failed")])
async def test_buffer_failure_does_not_retry(engine, api, definite, expected_state):
    api.failure = BufferError("transport_error", "Could not confirm Buffer's response.", definite=definite)
    draft = engine.preview("buffer:channel123", "hello")
    operation = engine.submit("buffer:channel123", draft["draft_id"], "same-request")
    await asyncio.gather(*engine.tasks)
    assert engine.status(operation["operation_id"])["state"] == expected_state
    assert engine.submit("buffer:channel123", draft["draft_id"], "same-request")["state"] == expected_state
    assert len(api.creates) == 1


async def test_restart_does_not_resubmit_uncertain_buffer_post(engine, api, store):
    draft = engine.preview("buffer:channel123", "hello")
    operation = engine.submit("buffer:channel123", draft["draft_id"], "same-request")
    await asyncio.gather(*engine.tasks)
    assert len(api.creates) == 1
    store.recover()
    assert engine.status(operation["operation_id"])["delivery_status"] == "accepted_by_buffer"
    assert len(api.creates) == 1


class FakeDirectPublisher:
    def __init__(self, state="succeeded"):
        self.previews = []
        self.submits = []
        self.state = state

    def preview(self, account_id, publication):
        self.previews.append((account_id, publication))
        return {"draft_id": "direct-draft-1", "publishable": True, "blockers": []}

    def submit(self, account_id, draft_id, idempotency_key):
        self.submits.append((account_id, draft_id, idempotency_key))
        return self.status("direct-operation-1")

    def status(self, operation_id):
        assert operation_id == "direct-operation-1"
        return {"operation_id": operation_id, "account_id": "123", "state": self.state,
                "phase": "complete" if self.state == "succeeded" else "stopped",
                "posts": ([{"id": "987", "url": "https://x.com/i/status/987",
                            "verified": self.state == "succeeded"}]
                          if self.state in {"succeeded", "partial"} else []),
                "error": ({"code": "unknown_outcome", "message": "Check X before retrying"}
                          if self.state == "unknown" else
                          {"code": "verification_failed", "message": "Do not repost"}
                          if self.state == "partial" else None)}


class FakeMediaStore:
    def __init__(self, failure=None):
        self.fetches = []
        self.failure = failure

    async def fetch(self, account_id, url, name, max_bytes=None):
        self.fetches.append((account_id, url, name, max_bytes))
        if self.failure:
            raise self.failure
        return {"media_id": "image-1", "kind": "image"}


def fallback_engine(store, api, *, state="succeeded", media_failure=None):
    store.set_buffer_direct_fallback_enabled(True)
    store.save_buffer_channels([{"account_id": "buffer:channel123", "channel_id": "channel123",
                                 "display_name": "X account", "handle": "@example",
                                 "x_account_id": "123"}])
    store.save_account("123", "example", {"auth_token": "fake"}, {})
    direct = FakeDirectPublisher(state)
    media = FakeMediaStore(media_failure)
    return BufferPublisher(store, api, direct_publisher=direct, media_store=media), direct, media


async def test_definite_buffer_quota_rejection_falls_back_once(store, api):
    api.failure = BufferError("rate_limited", "Buffer API quota exhausted.", definite=True)
    engine, direct, _ = fallback_engine(store, api)
    draft = engine.preview("buffer:channel123", "hello")
    first = engine.submit("buffer:channel123", draft["draft_id"], "same-request", "123")
    await asyncio.gather(*engine.tasks)
    status = engine.status(first["operation_id"])
    assert status["state"] == "succeeded"
    assert status["provider_used"] == "direct_x"
    assert status["fallback_used"] is True
    assert status["fallback_reason"] == "buffer_api_rate_limited"
    assert status["direct_operation_id"] == "direct-operation-1"
    assert status["direct_receipt"]["posts"][0]["id"] == "987"
    assert status["x_verified"] is True
    assert len(api.creates) == len(direct.submits) == 1
    again = engine.submit("buffer:channel123", draft["draft_id"], "same-request", "123")
    assert again["operation_id"] == first["operation_id"]
    assert len(api.creates) == len(direct.submits) == 1
    # A changed owner setting or client grant must not make an idempotent
    # receipt lookup fail and tempt the client to submit with a new key.
    assert engine.submit("buffer:channel123", draft["draft_id"], "same-request")["operation_id"] == first["operation_id"]
    assert len(api.creates) == len(direct.submits) == 1


@pytest.mark.parametrize("code,definite,expected_state", [
    ("rate_limited", False, "unknown"),
    ("limit_reached", True, "failed"),
    ("server_error", False, "unknown"),
])
async def test_fallback_never_runs_after_ambiguous_or_other_buffer_errors(
        store, api, code, definite, expected_state):
    api.failure = BufferError(code, "Buffer did not accept this post.", definite=definite)
    engine, direct, _ = fallback_engine(store, api)
    draft = engine.preview("buffer:channel123", "hello")
    operation = engine.submit("buffer:channel123", draft["draft_id"], "same-request", "123")
    await asyncio.gather(*engine.tasks)
    status = engine.status(operation["operation_id"])
    assert status["state"] == expected_state
    assert status["fallback_used"] is False
    assert status["direct_operation_id"] is None
    assert direct.submits == []
    assert len(api.creates) == 1


def test_fallback_requires_verified_same_x_account_and_share_now(store, api):
    engine, direct, _ = fallback_engine(store, api)
    store.save_account("999", "other", {"auth_token": "fake"}, {})
    draft = engine.preview("buffer:channel123", "hello")
    with pytest.raises(Problem) as mismatch:
        engine.submit("buffer:channel123", draft["draft_id"], "wrong-account", "999")
    assert mismatch.value.code == "fallback_account_mismatch"
    scheduled = engine.preview("buffer:channel123", "hello", [], "addToQueue")
    with pytest.raises(Problem) as mode:
        engine.submit("buffer:channel123", scheduled["draft_id"], "wrong-mode-1", "123")
    assert mode.value.code == "fallback_unsupported_mode"
    assert direct.submits == []
    assert api.creates == []


async def test_fallback_stages_bounded_public_images(store, api):
    api.failure = BufferError("rate_limited", "Buffer API quota exhausted.", definite=True)
    engine, direct, media = fallback_engine(store, api)
    draft = engine.preview("buffer:channel123", "hello", ["https://example.com/photo.png"])
    operation = engine.submit("buffer:channel123", draft["draft_id"], "image-request", "123")
    await asyncio.gather(*engine.tasks)
    assert engine.status(operation["operation_id"])["state"] == "succeeded"
    assert media.fetches == [("123", "https://example.com/photo.png", "buffer-image-1", 5 * 1024**2)]
    assert direct.previews[0][1].posts[0].attachments[0].media_id == "image-1"
    assert len(direct.submits) == 1


async def test_failed_image_staging_stops_before_direct_submission(store, api):
    api.failure = BufferError("rate_limited", "Buffer API quota exhausted.", definite=True)
    engine, direct, _ = fallback_engine(store, api,
        media_failure=Problem("media_too_large", "Image exceeds 5 MiB"))
    draft = engine.preview("buffer:channel123", "hello", ["https://example.com/photo.png"])
    operation = engine.submit("buffer:channel123", draft["draft_id"], "image-request", "123")
    await asyncio.gather(*engine.tasks)
    status = engine.status(operation["operation_id"])
    assert status["state"] == "failed"
    assert status["error"]["code"] == "media_too_large"
    assert direct.submits == []
    assert len(api.creates) == 1


async def test_unknown_direct_outcome_is_never_resubmitted(store, api):
    api.failure = BufferError("rate_limited", "Buffer API quota exhausted.", definite=True)
    engine, direct, _ = fallback_engine(store, api, state="unknown")
    draft = engine.preview("buffer:channel123", "hello")
    operation = engine.submit("buffer:channel123", draft["draft_id"], "same-request", "123")
    await asyncio.gather(*engine.tasks)
    status = await engine.refresh(operation["operation_id"])
    assert status["state"] == "unknown"
    assert status["delivery_status"] == "direct_outcome_unknown"
    assert status["x_verified"] is False
    assert engine.submit("buffer:channel123", draft["draft_id"], "same-request", "123")["state"] == "unknown"
    assert len(api.creates) == len(direct.submits) == 1


async def test_unverified_direct_post_is_not_reported_as_safe_to_retry(store, api):
    api.failure = BufferError("rate_limited", "Buffer API quota exhausted.", definite=True)
    engine, direct, _ = fallback_engine(store, api, state="partial")
    draft = engine.preview("buffer:channel123", "hello")
    operation = engine.submit("buffer:channel123", draft["draft_id"], "same-request", "123")
    await asyncio.gather(*engine.tasks)
    status = await engine.refresh(operation["operation_id"])
    assert status["state"] == "partial"
    assert status["delivery_status"] == "direct_outcome_unknown"
    assert status["direct_receipt"]["posts"][0]["id"] == "987"
    assert status["x_verified"] is False
    assert len(direct.submits) == 1


def test_buffer_draft_freezes_key_and_verified_x_identity(store, api):
    engine, _, _ = fallback_engine(store, api)
    draft = engine.preview("buffer:channel123", "hello")
    assert "_buffer_key_fingerprint" not in str(draft)
    store.save_buffer_key("replacement-key")
    store.save_buffer_channels([{"account_id": "buffer:channel123", "channel_id": "channel123",
                                 "display_name": "X account", "handle": "@example", "x_account_id": "123"}])
    with pytest.raises(Problem) as changed_key:
        engine.submit("buffer:channel123", draft["draft_id"], "request-after-key-change", "123")
    assert changed_key.value.code == "buffer_connection_changed"

    next_draft = engine.preview("buffer:channel123", "hello again")
    store.save_buffer_channels([{"account_id": "buffer:channel123", "channel_id": "channel123",
                                 "display_name": "Different X account", "handle": "@other", "x_account_id": "999"}])
    with pytest.raises(Problem) as changed_identity:
        engine.submit("buffer:channel123", next_draft["draft_id"], "request-after-identity-change")
    assert changed_identity.value.code == "buffer_connection_changed"


async def test_worker_rechecks_buffer_identity_before_network_write(store, api):
    engine, _, _ = fallback_engine(store, api)
    draft = engine.preview("buffer:channel123", "hello")
    operation = engine.submit("buffer:channel123", draft["draft_id"], "before-identity-change", "123")
    store.save_buffer_channels([{"account_id": "buffer:channel123", "channel_id": "channel123",
                                 "display_name": "Different X account", "handle": "@other", "x_account_id": "999"}])
    await asyncio.gather(*engine.tasks)
    status = engine.status(operation["operation_id"])
    assert status["state"] == "failed"
    assert status["error"]["code"] == "buffer_connection_changed"
    assert api.creates == []


async def test_disabling_fallback_during_image_staging_stops_direct_write(store, api):
    api.failure = BufferError("rate_limited", "Buffer API quota exhausted.", definite=True)
    engine, direct, media = fallback_engine(store, api)

    async def disable_during_fetch(*args, **kwargs):
        store.set_buffer_direct_fallback_enabled(False)
        return {"media_id": "image-1", "kind": "image"}

    media.fetch = disable_during_fetch
    draft = engine.preview("buffer:channel123", "hello", ["https://example.com/photo.png"])
    operation = engine.submit("buffer:channel123", draft["draft_id"], "disable-during-fetch", "123")
    await asyncio.gather(*engine.tasks)
    status = engine.status(operation["operation_id"])
    assert status["state"] == "failed"
    assert status["error"]["code"] == "fallback_disabled"
    assert direct.submits == []
    assert len(api.creates) == 1


async def test_fallback_uses_real_direct_publisher_operation(store, api):
    api.failure = BufferError("rate_limited", "Buffer API quota exhausted.", definite=True)
    engine, _, _ = fallback_engine(store, api)

    class DirectBackend:
        articles_ready = False
        creates = 0

        def __init__(self, cookies):
            assert cookies["auth_token"] == "fake"

        async def identity(self):
            return {"id": "123", "username": "example"}

        async def create_post(self, post, media_ids, poll, reply_to):
            DirectBackend.creates += 1
            assert post["text"] == "hello"
            assert media_ids == []
            return {"id": "987", "url": "https://x.com/i/status/987"}

        async def verify_post(self, post_id, account_id):
            return post_id == "987" and account_id == "123"

        async def close(self):
            pass

    engine.direct_publisher = Publisher(store, DirectBackend)
    draft = engine.preview("buffer:channel123", "hello")
    operation = engine.submit("buffer:channel123", draft["draft_id"], "same-request", "123")
    await asyncio.gather(*engine.tasks)
    status = await engine.refresh(operation["operation_id"])
    assert status["state"] == "succeeded"
    assert status["provider_used"] == "direct_x"
    assert status["x_verified"] is True
    assert status["direct_receipt"]["posts"][0]["id"] == "987"
    assert store.operation(status["direct_operation_id"])["account"] == "123"
    assert DirectBackend.creates == 1
    # A crash can happen after the child is committed but before its ID is
    # linked to the Buffer operation. Status recovery must only read the child.
    saved = json.loads(store.operation(operation["operation_id"])["result"])
    saved["direct_operation_id"] = None
    store.update_operation(operation["operation_id"], "unknown", saved, "restart_interrupted")
    recovered = await engine.refresh(operation["operation_id"])
    assert recovered["direct_operation_id"] == status["direct_operation_id"]
    assert recovered["state"] == "succeeded"
    assert DirectBackend.creates == 1
