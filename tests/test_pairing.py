import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import time

import httpx
import pytest

from x_publisher.app import create_app
from x_publisher.core import SCOPES, Store
from x_publisher.pairing import ENDPOINT, issue_pairing
from test_publisher import store


class SessionBackend:
    identity_calls = 0
    failure = None
    pause = False

    def __init__(self, cookies):
        self.cookies = cookies

    async def identity(self):
        type(self).identity_calls += 1
        if self.pause:
            await asyncio.sleep(0.05)
        if self.failure:
            raise self.failure
        return {"id": "1", "username": "premium"}

    def session_snapshot(self):
        return {**self.cookies, "ct0": "refreshed-csrf"}

    async def close(self):
        pass


@pytest.fixture
def backend():
    SessionBackend.identity_calls = 0
    SessionBackend.failure = None
    SessionBackend.pause = False
    return SessionBackend


async def send(c, grant, cookies=None, **kwargs):
    return await c.post(ENDPOINT, headers={"Authorization": "Pairing " + grant["pairing_token"]},
        json={"cookies": cookies or {"auth_token": "new-secret", "ct0": "new-csrf"}}, **kwargs)


async def test_pairing_encrypted_once_and_grants_unchanged(store, backend):
    app = create_app(store, "o"*64, backend)
    token = store.issue_token("desktop", SCOPES, [])
    grant = issue_pairing(store, "@Premium", "premium")
    assert grant["pairing_token"] not in str([tuple(r) for r in store.db.execute("SELECT * FROM pairings")])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as c:
        result = await send(c, grant)
        assert result.status_code == 200, result.text
        assert result.json()["account_id"] == "1"
        assert "new-secret" not in result.text
        assert store.session("1")["ct0"] == "refreshed-csrf"
        assert b"new-secret" not in store.account("1")["session"]
        assert not store.token_principal(token).accounts
        assert (await send(c, grant)).status_code == 401
        assert backend.identity_calls == 1


async def test_invalid_expired_replaced_and_revoked_grants(store, backend):
    app = create_app(store, "o"*64, backend)
    old = issue_pairing(store, "premium", "premium")
    current = issue_pairing(store, "premium", "premium")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as c:
        for scheme, token in [("Bearer", store.issue_token("desktop", SCOPES, ["1"])), ("Pairing", "x"*64)]:
            result = await c.post(ENDPOINT, headers={"Authorization": scheme+" "+token}, json={})
            assert result.status_code == 401
        assert (await send(c, old)).status_code == 401
        with store.db:
            store.db.execute("UPDATE pairings SET expires=?", (time.time()-1,))
        assert (await send(c, current)).status_code == 401
        grant = issue_pairing(store, "premium", "premium")
        with store.db:
            store.db.execute("DELETE FROM pairings")
        assert (await send(c, grant)).status_code == 401
        assert backend.identity_calls == 0


async def test_wrong_identity_and_stable_id_do_not_replace(store, backend):
    app = create_app(store, "o"*64, backend)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as c:
        grant = issue_pairing(store, "free", "free")
        result = await send(c, grant)
        assert result.json()["error"] == "account_mismatch"
        assert store.session("2")["auth_token"] == "secret-two"
        grant = issue_pairing(store, "premium", "premium")
        with store.db:
            store.db.execute("UPDATE pairings SET account_id='different'")
        assert (await send(c, grant)).json()["error"] == "account_mismatch"
        assert store.session("1")["auth_token"] == "secret-one"
        assert (await send(c, grant)).status_code == 401


async def test_bounds_origin_error_redaction_and_busy_account(store, backend):
    app = create_app(store, "o"*64, backend)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as c:
        grant = issue_pairing(store, "premium", "premium")
        headers = {"Authorization": "Pairing "+grant["pairing_token"], "Content-Type": "application/json"}
        assert (await c.post(ENDPOINT, headers={**headers, "Origin": "https://evil.example"}, json={})).status_code == 403
        assert (await c.post(ENDPOINT, headers=headers, content=b" "*16385)).status_code == 400
        assert (await send(c, grant)).status_code == 401
        grant = issue_pairing(store, "premium", "premium")
        assert (await send(c, grant, {"auth_token":"a", "ct0":"c", "unrelated":"secret"})).status_code == 400
        backend.failure = RuntimeError("new-secret https://secret.example/cookie")
        result = await send(c, issue_pairing(store, "premium", "premium"))
        assert result.status_code == 400 and "new-secret" not in result.text and "secret.example" not in result.text
        backend.failure = None
        lock = app.state.publisher.account_locks.setdefault("1", asyncio.Lock())
        async with lock:
            result = await send(c, issue_pairing(store, "premium", "premium"))
            assert result.json()["error"] == "account_busy"
        assert store.session("1")["auth_token"] == "secret-one"


async def test_concurrent_import_and_restart_persistence(store, backend):
    app = create_app(store, "o"*64, backend)
    grant = issue_pairing(store, "premium", "premium")
    # A second SQLite connection sees the committed pairing and can claim it only once.
    import sqlite3
    with sqlite3.connect(store.directory/'publisher.sqlite3') as db:
        assert db.execute("SELECT consumed FROM pairings").fetchone()[0] == 0
    backend.pause = True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as c:
        results = await asyncio.gather(send(c, grant), send(c, grant))
        assert sorted(r.status_code for r in results) == [200, 429]
        assert backend.identity_calls == 1
        assert (await send(c, grant)).status_code == 401


def helper_module():
    spec = importlib.util.spec_from_file_location("connect_x", Path(__file__).parents[1]/"connect/connect_x.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_helper_filters_cookies_and_rejects_redirect():
    h = helper_module()
    export = [{"domain":".x.com", "name":"auth_token", "value":"a"},
              {"domain":"x.com", "name":"ct0", "value":"c"},
              {"domain":"evil.com", "name":"auth_token", "value":"e"},
              {"domain":".x.com", "name":"unrelated", "value":"secret"}]
    assert h.selected_cookies(export) == {"auth_token":"a", "ct0":"c"}
    with pytest.raises(h.ConnectionFailure):
        h.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.com")


def test_helper_pairing_destination_and_secret_redaction(monkeypatch):
    h = helper_module()
    real_ssh = h.ssh_admin
    monkeypatch.setattr(h, "ssh_admin", lambda *_: json.dumps({"endpoint":"https://evil.com", "expected_user":"premium", "pairing_token":"s"*64}))
    with pytest.raises(h.ConnectionFailure, match="no cookies were sent"):
        h.pairing("example_user", "premium", "premium")
    class Failed:
        returncode = 1
        stdout = b"secret-cookie"
        stderr = b"secret-cookie"
    monkeypatch.setattr(h.subprocess, "run", lambda *a, **k: Failed())
    with pytest.raises(h.ConnectionFailure) as exc:
        # Restore the real helper to exercise subprocess failure handling.
        real_ssh("example_user", ["--help"])
    assert "secret-cookie" not in str(exc.value)
