"""Browser account setup must never bypass owner auth or account verification."""
import re
import time

import httpx
import pytest

from x_publisher.app import create_app
from x_publisher.browser_connect import BASE, BUFFER_SETTINGS, BUFFER_FALLBACK_SETTINGS, BUFFER_REFRESH, CLIENT_SETTINGS
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
    csrf = unescape(re.search(r'name="csrf" value="([^"]+)"', page.text).group(1))
    return csrf


def assert_no_vps_signin_offer(page):
    """The legacy browser routes may exist, but setup must not advertise them."""
    source = page.text.lower()
    assert "<iframe" not in source
    assert "novnc" not in source
    assert "open x sign-in" not in source
    assert "x login browser on your server" not in source
    assert 'action="' + BASE + '/start"' not in source
    assert '/view/vnc.html' not in source


async def test_setup_pages_share_owner_unlock_and_hide_vps_browser(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
        for path in (BASE, BUFFER_SETTINGS):
            page = await c.get(path)
            assert page.status_code == 200
            assert "Server owner key" in page.text
            assert 'name="key"' in page.text
            assert 'action="' + BASE + '/login"' in page.text
            assert_no_vps_signin_offer(page)
        await login(c)
        for path in (BASE, BUFFER_SETTINGS):
            page = await c.get(path)
            assert page.status_code == 200
            assert 'name="csrf"' in page.text
            assert_no_vps_signin_offer(page)


async def test_setup_pages_show_buffer_and_direct_account_status(app, store):
    store.save_buffer_key("buffer-test-key-123456789")
    store.save_buffer_channels([{"account_id": "buffer:chan-1", "channel_id": "chan-1",
                                "display_name": "Premium Buffer", "handle": "premium",
                                "x_account_id": "1"}])
    # The display must use saved verification information without requesting X again.
    checked = time.time()
    with store.db:
        store.db.execute("UPDATE accounts SET checked=? WHERE id='1'", (checked,))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
        await login(c)
        for path in (BASE, BUFFER_SETTINGS):
            page = await c.get(path)
            assert page.status_code == 200
            assert "Premium Buffer" in page.text
            assert "@premium" in page.text
            assert "Direct X" in page.text
            assert "last verified" in page.text.lower()
            assert "buffer-test-key-123456789" not in page.text
            assert "secret-one" not in page.text
            assert_no_vps_signin_offer(page)
            if path == BUFFER_SETTINGS:
                assert "Refresh channels" in page.text
            else:
                assert "Manage Buffer" in page.text
        assert SessionBackend.identity_calls == 0


async def test_saved_buffer_key_is_replaced_from_closed_disclosure(app, store):
    store.save_buffer_key("buffer-test-key-123456789")
    store.save_buffer_channels([{"account_id": "buffer:chan-1", "channel_id": "chan-1",
                                "display_name": "Premium Buffer", "handle": "premium",
                                "x_account_id": "1"}])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
        await login(c)
        page = await c.get(BUFFER_SETTINGS)
        assert page.status_code == 200
        disclosure = re.search(r"<details\b([^>]*)>(.*?)</details>", page.text, re.DOTALL | re.IGNORECASE)
        assert disclosure is not None
        assert not re.search(r"\bopen\b", disclosure.group(1), re.IGNORECASE)
        assert re.search(r"<summary[^>]*>\s*Replace(?: Buffer| API)? key\s*</summary>",
                         disclosure.group(2), re.IGNORECASE)
        assert 'name="api_key"' in disclosure.group(2)
        assert "buffer-test-key-123456789" not in page.text


async def test_account_labels_are_escaped_on_both_setup_pages(app, store):
    store.save_buffer_key("buffer-test-key-123456789")
    store.save_buffer_channels([{"account_id": "buffer:chan-1", "channel_id": "chan-1",
                                "display_name": '<img src=x onerror="alert(1)">',
                                "handle": "premium", "x_account_id": "1"}])
    with store.db:
        store.db.execute("UPDATE accounts SET username=? WHERE id='1'", ("<script>alert(2)</script>",))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
        await login(c)
        for path in (BASE, BUFFER_SETTINGS):
            page = await c.get(path)
            assert page.status_code == 200
            assert '<img src=x onerror="alert(1)">' not in page.text
            assert "<script>alert(2)</script>" not in page.text
            assert "&lt;img" in page.text
            assert "&lt;script&gt;" in page.text


async def test_browser_setup_requires_owner_and_stores_verified_session(app, store):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
        assert (await c.get(BASE)).status_code == 200
        assert (await c.post(BASE + "/start", data={"account": "new"}, headers={"Origin": ORIGIN})).status_code == 403
        assert (await c.post(BASE + "/login", data={"key": "wrong"}, headers={"Origin": ORIGIN})).status_code == 403
        csrf = await login(c)
        assert (await c.post(BASE + "/start", data={"account": "new", "csrf": "wrong"}, headers={"Origin": ORIGIN})).status_code == 403
        assert (await c.post(BASE + "/start", data={"account": "new", "csrf": csrf}, headers={"Origin": ORIGIN})).status_code == 303
        page = await c.get(BASE)
        assert_no_vps_signin_offer(page)
        assert "owner-test-key" not in page.text
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


async def test_hidden_browser_routes_handle_repeated_attempt_without_offering_vps_login(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
        first_csrf = await login(c)
        start = await c.post(BASE + "/start", data={"account": "new", "csrf": first_csrf},
                             headers={"Origin": ORIGIN})
        assert start.status_code == 303
        repeated = await c.post(BASE + "/start", data={"account": "new", "csrf": first_csrf},
                                headers={"Origin": ORIGIN})
        assert repeated.status_code == 303 and repeated.headers["location"] == BASE
        assert app.state.worker_calls == ["start"]
        other_csrf = await login(c)
        page = await c.get(BASE)
        assert_no_vps_signin_offer(page)
        assert "End previous sign-in" not in page.text
        reset = await c.post(BASE + "/reset", data={"csrf": other_csrf}, headers={"Origin": ORIGIN})
        assert reset.status_code == 303 and reset.headers["location"] == BASE
        assert app.state.worker_calls == ["start", "reset"]
        assert_no_vps_signin_offer(await c.get(BASE))


async def test_owner_can_allow_exact_client_callback_in_browser(app, store):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
        assert 'name="callback"' not in (await c.get(BASE)).text
        settings = await c.get(CLIENT_SETTINGS)
        assert settings.status_code == 200
        assert "Server owner key" in settings.text
        assert "Exact client callback URL" not in settings.text
        owner_login = await c.post(BASE + "/login", data={"key": "owner-test-key", "return_to": "client-settings"},
                                   headers={"Origin": ORIGIN})
        assert owner_login.status_code == 303
        assert owner_login.headers["location"] == CLIENT_SETTINGS
        assert 'name="callback"' not in (await c.get(BASE)).text
        settings = await c.get(CLIENT_SETTINGS)
        assert "Exact client callback URL" in settings.text
        assert 'name="csrf"' in settings.text
        from html import unescape
        import re
        csrf = unescape(re.search(r'name="csrf" value="([^"]+)"', settings.text).group(1))
        path = BASE + "/callback"
        headers = {"Origin": ORIGIN}
        bad = await c.post(path, data={"csrf": csrf, "callback": "https://evil.example/cb"}, headers=headers)
        assert bad.status_code == 400
        url = "http://127.0.0.1:43123/callback/Abcdefgh1234"
        good = await c.post(path, data={"csrf": csrf, "callback": url}, headers=headers)
        assert good.status_code == 200
        assert store.setting("callbacks") == ["http://127.0.0.1/callback/Abcdefgh1234"]


async def test_owner_can_connect_buffer_without_exposing_key(app, store, monkeypatch):
    import x_publisher.buffer_api as buffer_module

    class FakeBufferAPI:
        def __init__(self, key):
            assert key == "buffer-test-key-123456789"

        async def list_channels(self):
            return [
                {"id": "chan-1", "name": "My X account", "username": "myhandle",
                 "service": "twitter", "x_account_id": "1234567890123456789",
                 "isDisconnected": False, "isLocked": False},
                {"id": "chan-2", "name": "Unavailable", "username": "other",
                 "service": "twitter", "isDisconnected": True, "isLocked": False},
            ]

        async def close(self):
            pass

    monkeypatch.setattr(buffer_module, "BufferAPI", FakeBufferAPI)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
        assert "api_key" not in (await c.get(BUFFER_SETTINGS)).text
        assert (await c.post(BUFFER_SETTINGS, data={"api_key": "buffer-test-key-123456789"},
                             headers={"Origin": ORIGIN})).status_code == 403
        csrf = await login(c)
        wrong_origin = await c.post(BUFFER_SETTINGS,
                                    data={"csrf": csrf, "api_key": "buffer-test-key-123456789"},
                                    headers={"Origin": "https://other.example"})
        assert wrong_origin.status_code == 403
        result = await c.post(BUFFER_SETTINGS,
                              data={"csrf": csrf, "api_key": "buffer-test-key-123456789"},
                              headers={"Origin": ORIGIN})
        assert result.status_code == 303 and result.headers["location"].startswith(BUFFER_SETTINGS)
        assert store.buffer_key() == "buffer-test-key-123456789"
        assert b"buffer-test-key-123456789" not in (store.directory / "buffer-key.enc").read_bytes()
        assert [c["account_id"] for c in store.buffer_channels()] == ["buffer:chan-1"]
        assert store.buffer_channels()[0]["x_account_id"] == "1234567890123456789"
        page = await c.get(result.headers["location"])
        assert "My X account" in page.text and "buffer-test-key-123456789" not in page.text
        assert "Buffer connected" in page.text


async def test_invalid_buffer_key_shows_recovery_without_replacing_saved_connection(app, store, monkeypatch):
    import x_publisher.buffer_api as buffer_module

    store.save_buffer_key("buffer-original-key-123456789")
    store.save_buffer_channels([{"account_id": "buffer:chan-1", "channel_id": "chan-1",
                                "display_name": "Existing X channel", "handle": "premium",
                                "x_account_id": "1"}])

    class RejectedBufferAPI:
        def __init__(self, key):
            assert key == "buffer-invalid-key-123456789"

        async def list_channels(self):
            raise RuntimeError("buffer-invalid-key-123456789 upstream secret")

        async def close(self):
            pass

    monkeypatch.setattr(buffer_module, "BufferAPI", RejectedBufferAPI)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
        csrf = await login(c)
        response = await c.post(BUFFER_SETTINGS,
                                data={"csrf": csrf, "api_key": "buffer-invalid-key-123456789"},
                                headers={"Origin": ORIGIN})
        assert response.status_code == 400
        assert "<html" in response.text.lower()
        assert "Buffer" in response.text
        assert "try again" in response.text.lower()
        assert 'name="api_key"' in response.text
        assert "<details open>" in response.text
        assert "buffer-invalid-key-123456789" not in response.text
        assert "upstream secret" not in response.text
        assert store.buffer_key() == "buffer-original-key-123456789"
        assert store.buffer_channels()[0]["display_name"] == "Existing X channel"


async def test_invalid_buffer_channel_data_cannot_replace_a_working_key(app, store, monkeypatch):
    import x_publisher.buffer_api as buffer_module

    store.save_buffer_key("buffer-original-key-123456789")
    store.save_buffer_channels([{"account_id": "buffer:good", "channel_id": "good",
                                "display_name": "Working X channel", "handle": "premium"}])

    class InvalidChannels:
        def __init__(self, key):
            assert key == "buffer-new-key-123456789"

        async def list_channels(self):
            return [{"id": "invalid:id", "name": "Broken", "username": "premium",
                     "service": "twitter", "isDisconnected": False, "isLocked": False}]

        async def close(self):
            pass

    monkeypatch.setattr(buffer_module, "BufferAPI", InvalidChannels)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
        csrf = await login(c)
        response = await c.post(BUFFER_SETTINGS,
                                data={"csrf": csrf, "api_key": "buffer-new-key-123456789"},
                                headers={"Origin": ORIGIN})
        assert response.status_code == 400
        assert "<details open>" in response.text
        assert store.buffer_key() == "buffer-original-key-123456789"
        assert store.buffer_channels()[0]["display_name"] == "Working X channel"


async def test_refresh_channels_requires_owner_origin_csrf_and_preserves_key(app, store, monkeypatch):
    import x_publisher.buffer_api as buffer_module

    store.save_buffer_key("buffer-original-key-123456789")
    store.save_buffer_channels([{"account_id": "buffer:old", "channel_id": "old",
                                "display_name": "Old X channel", "handle": "old",
                                "x_account_id": "2"}])

    class FakeBufferAPI:
        def __init__(self, key):
            assert key == "buffer-original-key-123456789"

        async def list_channels(self):
            return [{"id": "new", "name": "New X channel", "username": "premium",
                     "service": "twitter", "x_account_id": "1", "isDisconnected": False,
                     "isLocked": False}]

        async def close(self):
            pass

    monkeypatch.setattr(buffer_module, "BufferAPI", FakeBufferAPI)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
        assert (await c.post(BUFFER_REFRESH, headers={"Origin": ORIGIN})).status_code == 403
        csrf = await login(c)
        assert (await c.post(BUFFER_REFRESH, data={"csrf": csrf},
                             headers={"Origin": "https://other.example"})).status_code == 403
        assert (await c.post(BUFFER_REFRESH, data={"csrf": "wrong"},
                             headers={"Origin": ORIGIN})).status_code == 403
        assert store.buffer_channels()[0]["display_name"] == "Old X channel"
        result = await c.post(BUFFER_REFRESH, data={"csrf": csrf}, headers={"Origin": ORIGIN})
        assert result.status_code == 303
        assert result.headers["location"].startswith(BUFFER_SETTINGS)
        assert store.buffer_key() == "buffer-original-key-123456789"
        assert [channel["channel_id"] for channel in store.buffer_channels()] == ["new"]
        page = await c.get(result.headers["location"])
        assert "Channels refreshed" in page.text
        assert "New X channel" in page.text


async def test_refresh_failure_keeps_saved_channels_and_redacts_upstream_error(app, store, monkeypatch):
    import x_publisher.buffer_api as buffer_module

    store.save_buffer_key("buffer-original-key-123456789")
    store.save_buffer_channels([{"account_id": "buffer:old", "channel_id": "old",
                                "display_name": "Existing X channel", "handle": "premium",
                                "x_account_id": "1"}])

    class FailingBufferAPI:
        def __init__(self, key):
            assert key == "buffer-original-key-123456789"

        async def list_channels(self):
            raise RuntimeError("buffer-original-key-123456789 upstream secret")

        async def close(self):
            pass

    monkeypatch.setattr(buffer_module, "BufferAPI", FailingBufferAPI)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
        csrf = await login(c)
        result = await c.post(BUFFER_REFRESH, data={"csrf": csrf}, headers={"Origin": ORIGIN})
        assert result.status_code == 502
        assert "Could not refresh channels" in result.text
        assert "Existing X channel" in result.text
        assert "buffer-original-key-123456789" not in result.text
        assert "upstream secret" not in result.text
        assert [channel["channel_id"] for channel in store.buffer_channels()] == ["old"]


async def test_owner_fallback_opt_in_requires_same_origin_and_csrf(app, store):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
        assert store.buffer_direct_fallback_enabled() is False
        denied = await c.post(BUFFER_FALLBACK_SETTINGS, data={"enabled": "1"},
                              headers={"Origin": ORIGIN})
        assert denied.status_code == 403
        csrf = await login(c)
        page = await c.get(BUFFER_SETTINGS)
        assert "direct X session if Buffer reaches its API limit" in page.text
        checkbox = re.search(r'<input\b(?=[^>]*name="enabled")[^>]*>', page.text, re.IGNORECASE)
        assert checkbox is not None and "checked" not in checkbox.group(0)
        wrong_origin = await c.post(BUFFER_FALLBACK_SETTINGS,
                                    data={"csrf": csrf, "enabled": "1"},
                                    headers={"Origin": "https://other.example"})
        assert wrong_origin.status_code == 403
        wrong_csrf = await c.post(BUFFER_FALLBACK_SETTINGS,
                                  data={"csrf": "wrong", "enabled": "1"},
                                  headers={"Origin": ORIGIN})
        assert wrong_csrf.status_code == 403
        assert store.buffer_direct_fallback_enabled() is False
        enabled = await c.post(BUFFER_FALLBACK_SETTINGS,
                               data={"csrf": csrf, "enabled": "1"},
                               headers={"Origin": ORIGIN})
        assert enabled.status_code == 303
        assert store.buffer_direct_fallback_enabled() is True
        checkbox = re.search(r'<input\b(?=[^>]*name="enabled")[^>]*>',
                             (await c.get(BUFFER_SETTINGS)).text, re.IGNORECASE)
        assert checkbox is not None and "checked" in checkbox.group(0)
        disabled = await c.post(BUFFER_FALLBACK_SETTINGS, data={"csrf": csrf},
                                headers={"Origin": ORIGIN})
        assert disabled.status_code == 303
        assert store.buffer_direct_fallback_enabled() is False
