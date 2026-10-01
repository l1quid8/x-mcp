"""One-use, administrator-issued session import grants. Never an MCP tool."""
import asyncio
import hashlib
import json
import logging
import traceback
from pathlib import Path
import re
import secrets
import time

from starlette.responses import JSONResponse
from .backend import capability_defaults, classify
from .extension_origin import EXTENSION_ORIGIN
from .core import ORIGIN, PREFIX, Problem

TIERS = ("unknown", "free", "basic", "premium", "premium_plus")
COOKIE_NAMES = {"auth_token", "ct0", "twid", "kdt", "att", "lang"}
ENDPOINT = ORIGIN + PREFIX + "/session-import"


def schema(store):
    store.db.execute('''CREATE TABLE IF NOT EXISTS pairings(
        hash TEXT PRIMARY KEY, username TEXT NOT NULL, account_id TEXT,
        tier TEXT NOT NULL, expires REAL NOT NULL, consumed INTEGER DEFAULT 0)''')
    store.db.commit()


def issue_pairing(store, username, tier, lifetime=900):
    username = username.lstrip("@").lower()
    if not re.fullmatch(r"[a-z0-9_]{1,15}", username) or tier not in TIERS:
        raise Problem("invalid_account", "Use a valid X username and account tier")
    if not 1 <= lifetime <= 900:
        raise ValueError("Pairing lifetime must be at most fifteen minutes")
    schema(store)
    account = store.db.execute("SELECT id FROM accounts WHERE lower(username)=?", (username,)).fetchone()
    token = secrets.token_urlsafe(48)
    expiry = time.time() + lifetime
    with store.db:
        store.db.execute("DELETE FROM pairings WHERE expires<=? OR username=?", (time.time(), username))
        store.db.execute("INSERT INTO pairings VALUES (?,?,?,?,?,0)",
            (hashlib.sha256(token.encode()).hexdigest(), username, account[0] if account else None, tier, expiry))
    return {"endpoint": ENDPOINT, "pairing_token": token, "expected_user": username, "expires": expiry}


def claim(store, token):
    with store.db:
        row = store.db.execute('''UPDATE pairings SET consumed=1
            WHERE hash=? AND consumed=0 AND expires>? RETURNING *''',
            (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
    if not row:
        raise Problem("invalid_pairing", "Pairing is invalid, expired, or already used; reconnect with the helper")
    return dict(row)


class SessionImporter:
    def __init__(self, store, backend_factory, account_locks):
        schema(store)
        self.store, self.backend_factory, self.account_locks = store, backend_factory, account_locks
        self.busy = False

    def record_failure(self, grant, backend, exc):
        # Only fixed structural diagnostics. Never exception messages, bodies,
        # cookie values, URLs, request headers, or stack locals.
        data = {"time": time.time(), "account": grant["username"],
            "exception": re.sub(r"[^A-Za-z0-9_]", "", type(exc).__name__)[:64],
            "stage": getattr(backend, "verification_stage", "session_import"),
            "frames": [{"file": Path(f.filename).name, "function": f.name, "line": f.lineno}
                       for f in traceback.extract_tb(exc.__traceback__)[-5:]]}
        self.store.set_setting("last_session_import_error", data)
        logging.getLogger("x_publisher.session").warning("Session import failed: exception=%s stage=%s", data["exception"], data["stage"])

    async def __call__(self, request):
        headers = {"Cache-Control": "no-store"}
        def response(data, code):
            return JSONResponse(data, status_code=code, headers=headers)
        if request.headers.get("origin") not in (None, ORIGIN, EXTENSION_ORIGIN):
            return response({"error": "invalid_origin"}, 403)
        values = request.headers.getlist("authorization")
        parts = values[0].split(" ", 1) if len(values) == 1 else []
        if len(parts) != 2 or parts[0] != "Pairing" or not re.fullmatch(r"[A-Za-z0-9_-]{64}", parts[1]):
            return response({"error": "invalid_pairing"}, 401)
        if self.busy:
            return response({"error": "import_busy", "message": "Another account import is in progress; try again shortly"}, 429)
        backend = None
        try:
            grant = claim(self.store, parts[1])
            self.busy = True
            # Authentication happens before reading credentials. No bodies or secrets are logged.
            if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                raise Problem("invalid_payload", "Expected a JSON session from the connection helper")
            body = bytearray()
            async with asyncio.timeout(15):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > 16384:
                        raise Problem("invalid_payload", "Session payload is too large")
                    body.extend(chunk)
            data = json.loads(body)
            if not isinstance(data, dict) or set(data) != {"cookies"}:
                raise Problem("invalid_payload", "Expected only session cookies")
            cookies = data["cookies"]
            if (not isinstance(cookies, dict) or set(cookies) - COOKIE_NAMES
                or not all(isinstance(v, str) and 0 < len(v) <= 4096 for v in cookies.values())
                or not cookies.get("auth_token") or not cookies.get("ct0")):
                raise Problem("invalid_payload", "Missing or invalid X session cookies")
            backend = self.backend_factory(cookies)
            async with asyncio.timeout(90):
                identity = await backend.identity()
            aid, username = identity["id"], identity["username"]
            if (not isinstance(aid, str) or not aid.isdigit() or not isinstance(username, str)
                or username.lower() != grant["username"]
                or (grant["account_id"] and aid != grant["account_id"])):
                raise Problem("account_mismatch", "Signed-in X identity does not match the account selected for pairing")
            lock = self.account_locks.setdefault(aid, asyncio.Lock())
            if lock.locked():
                raise Problem("account_busy", "This account is publishing; reconnect after the operation finishes")
            async with lock:
                existing = self.store.db.execute("SELECT capabilities FROM accounts WHERE id=?", (aid,)).fetchone()
                caps = capability_defaults(grant["tier"])
                if existing:
                    previous = json.loads(existing[0])
                    caps["verified_formats"] = previous.get("verified_formats", {})
                self.store.save_account(aid, username, backend.session_snapshot(), caps)
            return response({"account_id": aid, "username": username, "session_verified": True,
                "tier_source": "owner_attested", "publishing_verified": False,
                "message": "Session stored encrypted. Existing client account grants are unchanged."}, 200)
        except Problem as exc:
            if backend is not None:
                self.record_failure(grant, backend, exc)
            return response({"error": exc.code, "message": exc.message}, 401 if exc.code == "invalid_pairing" else 400)
        except (ValueError, TypeError, KeyError) as exc:
            if backend is not None:
                self.record_failure(grant, backend, exc)
            return response({"error": "invalid_session", "message": "Session could not be verified; reconnect using the helper"}, 400)
        except Exception as exc:
            if backend is not None:
                self.record_failure(grant, backend, exc)
            code, _ = classify(exc)
            return response({"error": code, "message": "X session verification failed. Resolve any X challenge and reconnect; no session was imported."}, 400)
        finally:
            # Only the request that claimed the grant can release the import slot.
            if 'grant' in locals():
                self.busy = False
            if backend is not None:
                try:
                    await backend.close()
                except Exception:
                    pass
