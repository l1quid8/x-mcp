"""Browser account setup must never bypass owner auth or account verification."""
import httpx
import pytest

from x_publisher.app import create_app
from x_publisher.browser_connect import BASE
from x_publisher.core import ORIGIN
from test_pairing import SessionBackend
from test_publisher import store


@pytest.fixture
def app(store, monkeypatch):
    import x_publisher.browser_connect as module
    monkeypatch.setattr(module, "WORKER", "http://browser:8785")
    monkeypatch.setattr(module, "VIEW", "http://browser:6080")
    monkeypatch.setattr(module, "TOKEN", "t" * 48)
    SessionBackend.identity_calls = 0
    SessionBackend.failure = None
    SessionBackend.pause = False
    result = create_app(store, "owner-test-key", SessionBackend)
    calls = []

    async def worker(action, session):
        calls.append(action)
        if action == "finish":
            return httpx.Response(200, json={"cookies": {"auth_token": "new-secret", "ct0": "new-csrf"}})
        return httpx.Response(200, json={"state": "ready"})

    monkeypatch.setattr(result.state.browser_connect, "worker", worker)
    result.state.worker_calls = calls
    return result


async def login(client):
    response = await client.post(BASE + "/login", data={"key": "owner-test-key"},
                                 headers={"Origin": ORIGIN})
    assert response.status_code == 303
    page = await client.get(BASE)
    assert page.status_code == 200
    from html import unescape
    import re
    csrf = unescape(re.search(r'name="csrf" value="([^"]+)"', page.text).group(1))
    return csrf


async def test_browser_setup_requires_owner_and_stores_verified_session(app, store):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
        assert (await c.get(BASE)).status_code == 200
        assert (await c.post(BASE + "/start", data={"account": "new"}, headers={"Origin": ORIGIN})).status_code == 403
        assert (await c.post(BASE + "/login", data={"key": "wrong"}, headers={"Origin": ORIGIN})).status_code == 403
        csrf = await login(c)
        assert (await c.post(BASE + "/start", data={"account": "new", "csrf": "wrong"}, headers={"Origin": ORIGIN})).status_code == 403
        assert (await c.post(BASE + "/start", data={"account": "new", "csrf": csrf}, headers={"Origin": ORIGIN})).status_code == 303
        page = await c.get(BASE)
        assert "iframe" in page.text and "owner-test-key" not in page.text
        assert (await c.post(BASE + "/finish", data={"csrf": csrf}, headers={"Origin": "https://evil.example"})).status_code == 403
        result = await c.post(BASE + "/finish", data={"csrf": csrf}, headers={"Origin": ORIGIN})
        assert result.status_code == 200
        assert "Connected @premium" in result.text
        assert store.session("1")["auth_token"] == "new-secret"
        assert b"new-secret" not in store.account("1")["session"]
        assert app.state.worker_calls == ["start", "finish"]
        assert SessionBackend.identity_calls == 1


async def test_reconnect_rejects_different_identity_and_cancel_closes(app, store):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
        csrf = await login(c)
        response = await c.post(BASE + "/start", data={"account": "2", "csrf": csrf}, headers={"Origin": ORIGIN})
        assert response.status_code == 303
        result = await c.post(BASE + "/finish", data={"csrf": csrf}, headers={"Origin": ORIGIN})
        assert result.status_code == 400
        assert store.session("2")["auth_token"] != "new-secret"
        assert (await c.post(BASE + "/start", data={"account": "new", "csrf": csrf}, headers={"Origin": ORIGIN})).status_code == 303
        assert (await c.post(BASE + "/cancel", data={"csrf": csrf}, headers={"Origin": ORIGIN})).status_code == 303
        assert app.state.worker_calls == ["start", "finish", "start", "stop"]


async def test_owner_can_allow_exact_client_callback_in_browser(app, store):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
        csrf = await login(c)
        path = BASE + "/callback"
        headers = {"Origin": ORIGIN}
        bad = await c.post(path, data={"csrf": csrf, "callback": "https://evil.example/cb"}, headers=headers)
        assert bad.status_code == 400
        url = "http://127.0.0.1:43123/callback/Abcdefgh1234"
        good = await c.post(path, data={"csrf": csrf, "callback": url}, headers=headers)
        assert good.status_code == 200
        assert store.setting("callbacks") == ["http://127.0.0.1/callback/Abcdefgh1234"]
