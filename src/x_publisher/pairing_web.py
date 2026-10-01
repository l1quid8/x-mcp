"""Browser-approved pairing: public HTTPS entry point, independent owner authentication."""
import hmac
import hashlib
import html
import json
import re
import secrets
import time
from collections import deque
from urllib.parse import parse_qs

from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route
from .extension_origin import EXTENSION_ORIGIN
from .core import ORIGIN, PREFIX
from .pairing import TIERS, issue_pairing

BASE = PREFIX + "/oauth/pair"
COOKIE = "__Secure-xpublisher-pair"


class BrowserPairing:
    def __init__(self, oauth):
        self.oauth = oauth
        self.attempts = {"start": deque(), "approve": deque()}

    def headers(self):
        # Native form POSTs send Origin: null under no-referrer. Preserve only
        # the HTTPS origin, never the request path/query, while retaining strict CSRF checks.
        return {"Cache-Control": "no-store", "Referrer-Policy": "strict-origin",
            "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"}

    def result(self, data, status=200):
        return JSONResponse(data, status_code=status, headers=self.headers())

    def limited(self, kind):
        bucket = self.attempts[kind]
        now = time.monotonic()
        while bucket and bucket[0] < now - 60:
            bucket.popleft()
        if len(bucket) >= 10:
            return True
        bucket.append(now)
        return False

    async def body(self, request):
        result = bytearray()
        async for chunk in request.stream():
            if len(result) + len(chunk) > 8192:
                raise ValueError("Body too large")
            result.extend(chunk)
        return result

    async def start(self, request):
        if request.headers.get("origin") not in (None, ORIGIN, EXTENSION_ORIGIN):
            return self.result({"error": "invalid_origin"}, 403)
        if self.limited("start"):
            return self.result({"error": "rate_limited"}, 429)
        try:
            data = json.loads(await self.body(request))
            account, tier = data["account"], data["tier"]
            if not re.fullmatch(r"[a-z0-9_]{1,15}", account) or tier not in TIERS:
                raise ValueError()
            device, public = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
            alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
            code = ''.join(secrets.choice(alphabet) for _ in range(8))
            code = code[:4] + "-" + code[4:]
            self.oauth.put("pair-device", device, {"request": public}, 600)
            self.oauth.put("pair-request", public, {"account": account, "tier": tier,
                "user_code": code, "approved": False}, 600)
            return self.result({"device_code": device, "user_code": code,
                "verification_uri": ORIGIN+BASE+"/approve?request="+public, "expires_in": 600, "interval": 2})
        except Exception:
            return self.result({"error": "invalid_request"}, 400)

    @staticmethod
    def cookie_name(public):
        # Concurrent approval tabs must not overwrite each other's CSRF cookie.
        return COOKIE + "-" + hashlib.sha256(public.encode()).hexdigest()[:16]

    async def approve(self, request):
        if request.method == "GET":
            public = request.query_params.get("request", "")
            data = self.oauth.get("pair-request", public)
            if not data or data["approved"]:
                return HTMLResponse("Pairing expired or already approved. Run the connection helper again.", status_code=400, headers=self.headers())
            nonce = secrets.token_urlsafe(32)
            # Separate short-lived CSRF record avoids extending the device request's lifetime.
            self.oauth.put("pair-csrf", nonce, {"request": public}, 600)
            escape = html.escape
            response = HTMLResponse('''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Connect X account</title><style>body{font:18px system-ui;max-width:560px;margin:50px auto;padding:24px}input,button{font:inherit;padding:12px;box-sizing:border-box;max-width:100%}label{display:block;margin:20px 0}code{font-size:28px}</style><h1>Connect @'''+escape(data['account'])+'''</h1><p>Check that this code matches the connection helper on your computer:</p><p><code>'''+escape(data['user_code'])+'''</code></p><p>This permits one session import for this account. It does not publish anything or expand MCP client permissions.</p><form method="post"><input type="hidden" name="request" value="'''+escape(public,quote=True)+'''"><label>Publisher owner key<br><input type="password" name="key" required maxlength="256" autocomplete="off"></label><label><input type="checkbox" name="match" value="yes" required> This code matches my helper.</label><button>Allow account connection</button></form><p>Enter your publisher owner key here. Your X password belongs only on x.com.</p></html>''', headers=self.headers())
            response.set_cookie(self.cookie_name(public), nonce, secure=True, httponly=True, samesite="strict", path=BASE+"/approve", max_age=600)
            return response
        if request.headers.get("origin") != ORIGIN:
            return self.result({"error": "invalid_origin"}, 403)
        if self.limited("approve"):
            return self.result({"error": "rate_limited"}, 429)
        try:
            form = parse_qs((await self.body(request)).decode(), keep_blank_values=True)
            public = form.get("request", [""])[0]
            csrf = self.oauth.get("pair-csrf", request.cookies.get(self.cookie_name(public), ""), consume=True)
            if not csrf or csrf["request"] != public:
                return self.result({"error": "approval_session_expired", "message": "This approval page expired or was already submitted. Start a fresh connection from the extension. Your owner key has not changed."}, 403)
            if form.get("match") != ["yes"]:
                return self.result({"error": "code_confirmation_required", "message": "Reload this page and confirm that the pairing code matches the extension."}, 400)
            supplied = form.get("key", [""])[0].strip()
            if not hmac.compare_digest(supplied.encode(), self.oauth.owner_key.encode()):
                return self.result({"error": "owner_key_mismatch", "message": "The X MCP owner key does not match. Reload this page and use the configured owner key."}, 403)
            data = self.oauth.get("pair-request", public, consume=True)
            if not data or data["approved"]:
                return self.result({"error": "invalid_pairing"}, 400)
            data["approved"] = True
            self.oauth.put("pair-request", public, data, 120)
            response = HTMLResponse("<h1>Connection allowed</h1><p>Return to your helper to finish connecting X. You can close this page.</p>", headers=self.headers())
            response.delete_cookie(self.cookie_name(public), path=BASE+"/approve", secure=True, httponly=True, samesite="strict")
            return response
        except Exception:
            return self.result({"error": "invalid_request"}, 400)

    async def poll(self, request):
        if request.headers.get("origin") not in (None, ORIGIN, EXTENSION_ORIGIN):
            return self.result({"error": "invalid_origin"}, 403)
        values = request.headers.getlist("authorization")
        parts = values[0].split(" ", 1) if len(values) == 1 else []
        if len(parts) != 2 or parts[0] != "Device" or not re.fullmatch(r"[A-Za-z0-9_-]{64}", parts[1]):
            return self.result({"error": "unauthorized"}, 401)
        device = self.oauth.get("pair-device", parts[1])
        data = self.oauth.get("pair-request", device["request"]) if device else None
        if not data:
            return self.result({"error": "expired_or_used"}, 401)
        if not data["approved"]:
            return self.result({"state": "pending"}, 202)
        self.oauth.get("pair-request", device["request"], consume=True)
        self.oauth.get("pair-device", parts[1], consume=True)
        return self.result(issue_pairing(self.oauth.store, data["account"], data["tier"]))


def browser_pairing_routes(oauth):
    flow = BrowserPairing(oauth)
    return [Route(BASE+"/start", flow.start, methods=["POST"]),
            Route(BASE+"/approve", flow.approve, methods=["GET", "POST"]),
            Route(BASE+"/poll", flow.poll, methods=["POST"])]
