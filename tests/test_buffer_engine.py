import asyncio
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet
import pytest

from x_publisher.buffer_api import BufferError
from x_publisher.buffer_engine import BufferPublisher
from x_publisher.core import Problem, Store


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
