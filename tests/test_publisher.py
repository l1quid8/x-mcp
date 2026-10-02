import asyncio
import hashlib
import io
import json
import time

from cryptography.fernet import Fernet
import httpx
from PIL import Image
from pydantic import ValidationError
import pytest
from starlette.testclient import TestClient

from x_publisher.app import create_app
from x_publisher.backend import capability_defaults, XBackend
from x_publisher.core import Publication, Problem, SCOPES, Store
from x_publisher.engine import Publisher
from x_publisher.media import MediaStore, public_url


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path, Fernet.generate_key())
    s.save_account("1", "premium", {"auth_token":"secret-one", "ct0":"csrf-one"}, capability_defaults("premium"))
    s.save_account("2", "free", {"auth_token":"secret-two", "ct0":"csrf-two"}, capability_defaults("free"))
    yield s
    s.db.close()


class Backend:
    articles_ready = False
    calls = []
    failure = None
    fail_at = 0

    def __init__(self, cookies):
        self.account = "1" if cookies["auth_token"] == "secret-one" else "2"

    async def identity(self):
        return {"id":self.account,"username":"test"}

    async def close(self):
        pass

    async def create_post(self, post, media, poll, reply):
        await asyncio.sleep(0.01)
        if self.failure and len(self.calls) == self.fail_at:
            raise self.failure
        self.calls.append((self.account, post["text"], reply))
        pid = str(100 + len(self.calls))
        return {"id":pid,"url":"https://x.com/i/status/"+pid}

    async def verify_post(self, pid, account):
        return account == self.account


@pytest.fixture
def engine(store):
    Backend.calls = []
    Backend.failure = None
    Backend.fail_at = 0
    return Publisher(store, Backend)


def post(text="hello"):
    return Publication(kind="post", posts=[{"text":text}])


async def done(engine):
    await asyncio.gather(*engine.tasks)


def test_sessions_encrypted_and_account_limits(store, engine):
    assert b"secret-one" not in store.account("1")["session"]
    assert store.session("1")["ct0"] == "csrf-one"
    p = Publication(kind="post", posts=[{"text":"x"*300,"long_post":True}])
    assert engine.preview("1", p)["publishable"]
    with pytest.raises(Problem, match="limit|verified"):
        engine.preview("2", p)


async def test_preview_never_publishes_and_idempotency(engine):
    d = engine.preview("1", post())
    assert not Backend.calls
    a = engine.submit("1", d["draft_id"], "request-123")
    b = engine.submit("1", d["draft_id"], "request-123")
    assert a["operation_id"] == b["operation_id"]
    await done(engine)
    assert len(Backend.calls) == 1
    assert engine.status(a["operation_id"])["state"] == "succeeded"
    d2 = engine.preview("1", post("different"))
    with pytest.raises(Problem, match="different"):
        engine.submit("1", d2["draft_id"], "request-123")


async def test_timeout_does_not_retry(engine):
    Backend.failure = httpx.ReadTimeout("sensitive URL must not appear")
    d = engine.preview("1", post())
    op = engine.submit("1", d["draft_id"], "request-timeout")
    await done(engine)
    result = engine.status(op["operation_id"])
    assert result["state"] == "unknown"
    assert "sensitive" not in json.dumps(result)
    assert engine.submit("1", d["draft_id"], "request-timeout")["state"] == "unknown"
    assert not engine.tasks


@pytest.mark.parametrize("name,code",[("Unauthorized","session_or_account_restricted"),("TooManyRequests","rate_limited")])
async def test_expired_session_and_rate_limit_stop(engine,name,code):
    Backend.failure = type(name,(Exception,),{})("secret upstream message")
    d = engine.preview("1",post())
    op = engine.submit("1",d["draft_id"],"request-error")
    await done(engine)
    result = engine.status(op["operation_id"])
    assert result["state"] == "failed"
    assert result["error"]["code"] == code
    assert "secret" not in json.dumps(result)
    assert not Backend.calls


async def test_thread_partial_receipts(engine):
    Backend.failure = Problem("x_rejected", "Rejected")
    Backend.fail_at = 1
    d = engine.preview("1", Publication(kind="thread",posts=[{"text":"one"},{"text":"two"}]))
    op = engine.submit("1", d["draft_id"], "request-thread")
    await done(engine)
    result = engine.status(op["operation_id"])
    assert result["state"] == "partial"
    assert len(result["posts"]) == 1 and result["posts"][0]["verified"]
    assert result["unsent_entries"] == 1


async def test_thread_serialization(engine):
    a = engine.preview("1", Publication(kind="thread",posts=[{"text":"a1"},{"text":"a2"}]))
    b = engine.preview("1", post("b1"))
    engine.submit("1", a["draft_id"], "request-a1")
    engine.submit("1", b["draft_id"], "request-b1")
    await done(engine)
    assert [c[1] for c in Backend.calls] == ["a1","a2","b1"]
    assert Backend.calls[1][2] == "101"


def test_article_honesty(engine):
    d = engine.preview("1", Publication(kind="article",article={"title":"Hello","markdown":"# Heading\nBody"}))
    assert not d["publishable"]
    assert d["blockers"][0]["code"] == "article_backend_unverified"
    with pytest.raises(Problem, match="validated"):
        engine.submit("1", d["draft_id"], "request-article")


def test_weighted_text_validation(engine):
    with pytest.raises(Problem, match="limit"):
        engine.preview("2", post("界"*141))
    assert engine.preview("2", post("https://example.com/"+"a"*600))["publishable"]
    assert engine.preview("2", post("😀"*140))["publishable"]


def test_restart_keeps_uncertain_receipts(store):
    store.db.execute("INSERT INTO operations VALUES ('op','1','key','hash','{}','running','{\"posts\":[{\"id\":\"99\"}]}','submitting',0,0)")
    store.db.commit()
    store.recover()
    assert store.operation("op")["state"] == "unknown"
    assert "99" in store.operation("op")["result"]


def png():
    out = io.BytesIO()
    Image.new("RGB",(8,8)).save(out,format="PNG")
    return out.getvalue()


async def chunks(data):
    yield data


async def test_upload_resume_checksum_and_isolation(store):
    m = MediaStore(store)
    data = png()
    u = m.begin("1","../../test.png",len(data),hashlib.sha256(data).hexdigest())
    mid = u["media_id"]
    await m.append(mid,"1",0,chunks(data[:20]))
    with pytest.raises(Problem,match="offset"):
        await m.append(mid,"1",0,chunks(data))
    with pytest.raises(Problem,match="another account"):
        await m.append(mid,"2",20,chunks(data[20:]))
    await m.append(mid,"1",20,chunks(data[20:]))
    assert (await m.finish(mid,"1"))["kind"] == "image"
    assert (await m.finish(mid,"1"))["kind"] == "image"
    assert store.media(mid,"1")["name"] == "test.png"


async def test_interrupted_chunk_and_quota(store):
    m = MediaStore(store,quota=100)
    u = m.begin("1","a",80)
    with pytest.raises(Problem,match="quota"):
        m.begin("1","b",30)
    async def broken():
        yield b"abc"
        raise ConnectionError()
    with pytest.raises(ConnectionError):
        await m.append(u["media_id"],"1",0,broken())
    assert store.media(u["media_id"],"1")["offset"] == 0
    assert (store.media_directory/u["media_id"]).stat().st_size == 0


@pytest.mark.parametrize("content_length", [6, None])
async def test_fallback_image_download_is_capped_and_leaves_no_staging_file(store, monkeypatch, content_length):
    from x_publisher import media as media_module

    class FakeResolver:
        async def close(self):
            pass

    class FakeResponse:
        status = 200
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        @property
        def content_length(self):
            return content_length
        @property
        def content(self):
            return self
        async def iter_chunked(self, size):
            yield b"1234"
            yield b"56"

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def get(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(media_module, "PublicResolver", FakeResolver)
    monkeypatch.setattr(media_module.aiohttp, "TCPConnector", lambda **kwargs: object())
    monkeypatch.setattr(media_module.aiohttp, "ClientSession", FakeSession)
    with pytest.raises(Problem) as error:
        await MediaStore(store).fetch("1", "https://images.example.com/large.png", max_bytes=5)
    assert error.value.code == "media_too_large"
    assert store.db.execute("SELECT count(*) FROM media").fetchone()[0] == 0
    assert list(store.media_directory.iterdir()) == []


@pytest.mark.parametrize("url",["http://example.com/a","https://localhost/a","https://127.0.0.1/a","https://[::1]/a","https://169.254.169.254/a","https://example.com:8080/a","https://user:password@example.com/a"])
def test_reject_private_media(url):
    with pytest.raises(Problem):
        public_url(url)


async def rpc(client, token, method, params=None, rid=1):
    return await client.post("/x-mcp/mcp",headers={"Authorization":"Bearer "+token,"Accept":"application/json, text/event-stream"},json={"jsonrpc":"2.0","id":rid,"method":method,"params":params or {}})


async def test_http_auth_tools_and_scopes(store):
    token = store.issue_token("test",SCOPES,["1"])
    restricted = store.issue_token("restricted",["publisher:status"],["1"])
    app = create_app(store, "a"*64, Backend)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://mcp.example.test") as c:
        assert (await c.post("/x-mcp/mcp")).status_code == 401
        assert (await rpc(c,"wrong","initialize")).status_code == 401
        response = await rpc(c,token,"initialize",{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"test","version":"1"}})
        assert response.status_code == 200, response.text
        tools = (await rpc(c,token,"tools/list")).json()["result"]["tools"]
        assert len(tools) == 23
        assert 'preview_post_deletion' in {tool['name'] for tool in tools}
        assert {"publishing_status", "stage_media", "begin_media_upload", "preview_publication", "publish_publication", "publication_status"} <= {t["name"] for t in tools}
        files = next(t for t in tools if t["name"] == "stage_media")
        assert files["_meta"]["openai/fileParams"] == ["files"]
        result = (await rpc(c,token,"tools/call",{"name":"publishing_status","arguments":{}})).json()["result"]
        assert not result.get("isError"), result
        assert result["structuredContent"]["accounts"][0]["account_id"] == "1"
        denied = (await rpc(c,token,"tools/call",{"name":"preview_publication","arguments":{"account_id":"2","content":{"kind":"post","posts":[{"text":"hi"}]}}})).json()["result"]
        assert denied["isError"]
        assert "account_not_authorized" in denied["content"][0]["text"]
        denied = (await rpc(c,restricted,"tools/call",{"name":"preview_publication","arguments":{"account_id":"1","content":{"kind":"post","posts":[{"text":"hi"}]}}})).json()["result"]
        assert denied["isError"]
        with store.db:
            store.db.execute("UPDATE tokens SET active=0")
        assert (await rpc(c,token,"tools/list")).status_code == 401
