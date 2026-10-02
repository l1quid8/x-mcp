"""Independent owner authorization built on the MCP SDK's OAuth/PKCE routes."""
import hashlib
import hmac
import html
import json
import re
import secrets
import sqlite3
import time
from urllib.parse import parse_qs, urlsplit

from mcp.server.auth.provider import AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError, RefreshToken, RegistrationError, TokenError, construct_redirect_uri
from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from .core import ACCOUNT_SCOPES, BUFFER_SCOPES, DEFAULT_SCOPES, ISSUER, ORIGIN, PREFIX, RESOURCE, SCOPES, X_ACCOUNT_SCOPES, Principal, Problem, canonical

METADATA = ORIGIN + "/.well-known/oauth-protected-resource" + PREFIX + "/mcp"
COOKIE = "__Secure-xmcp-consent"
ACCOUNT_PERMISSIONS = {
    "buffer:status": "View configured Buffer X channel status",
    "buffer:publish": "Create posts through a selected Buffer X channel",
    "publisher:status": "View connected account status",
    "publisher:media": "Stage media for a selected account",
    "publisher:publish": "Publish reviewed content to a selected account",
    "cleanup:read": "Check cleanup readiness and read content, plans and audit history",
    "cleanup:plan": "Prepare and review deletion plans",
    "cleanup:execute": "Run dry runs and submit approved deletion plans (requires server enablement)",
    "cleanup:protect": "Change cleanup protection rules",
}
EXPLICIT_CONSENT_SCOPES = frozenset({"buffer:publish", "publisher:publish", "cleanup:execute", "cleanup:protect"})


def callback_key(value):
    """Match an approved callback; loopback ports may vary between Codex logins."""
    try:
        parsed = urlsplit(str(value))
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            return None
        if (parsed.scheme == "https" and parsed.hostname == "chatgpt.com"
                and parsed.port in (None, 443)
                and (parsed.path.startswith("/connector/oauth/")
                     or parsed.path == "/connector_platform_oauth_redirect")):
            return str(value)
        if (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                and (parsed.port is None or 1 <= parsed.port <= 65535)
                and re.fullmatch(r"/callback(?:/[A-Za-z0-9_-]{8,64})?", parsed.path)):
            return "http://127.0.0.1" + parsed.path
    except ValueError:
        pass
    return None


class OwnerOAuth:
    def __init__(self, store, owner_key):
        self.store, self.owner_key = store, owner_key
        self.key = hashlib.sha256(("x-mcp-oauth:" + owner_key).encode()).digest()
        path = store.directory / "oauth.sqlite3"
        self.db = sqlite3.connect(path, timeout=15)
        path.chmod(0o600)
        self.db.execute("CREATE TABLE IF NOT EXISTS records(kind TEXT,key TEXT,value TEXT,expires REAL,PRIMARY KEY(kind,key))")
        self.db.commit()

    def digest(self, value):
        return hmac.new(self.key, value.encode(), hashlib.sha256).hexdigest()

    def put(self, kind, key, value, lifetime):
        with self.db:
            self.db.execute("DELETE FROM records WHERE expires<=?", (time.time(),))
            if self.db.execute("SELECT COUNT(*) FROM records WHERE kind=?", (kind,)).fetchone()[0] >= 1000:
                raise ValueError("OAuth capacity reached")
            self.db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?)", (kind, self.digest(key), canonical(value), time.time()+lifetime))

    def get(self, kind, key, consume=False):
        with self.db:
            if consume:
                self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT value,expires FROM records WHERE kind=? AND key=?", (kind, self.digest(key))).fetchone()
            if consume:
                self.db.execute("DELETE FROM records WHERE kind=? AND key=?", (kind, self.digest(key)))
        return json.loads(row[0]) if row and row[1] > time.time() else None

    def headers(self):
        destinations = " ".join(self.store.setting("callbacks", []))
        return {"Cache-Control": "no-store", "Referrer-Policy": "strict-origin", "X-Frame-Options": "DENY",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": f"default-src 'none'; style-src 'unsafe-inline'; form-action 'self' {destinations}; frame-ancestors 'none'; base-uri 'none'"}

    async def get_client(self, client_id):
        data = self.get("client", client_id)
        if not data:
            return None
        client = OAuthClientInformationFull.model_validate(data)
        # Registration describes what a client may REQUEST, not an owner's grant.
        # Older clients must be able to request newly supported scopes at consent.
        # Access/refresh grants remain unchanged, and omitted scopes use read-only defaults.
        client.scope = " ".join(SCOPES)
        return client

    async def register_client(self, client_info):
        allowed = self.store.setting("callbacks", [])
        if not client_info.redirect_uris or any(callback_key(u) not in allowed for u in client_info.redirect_uris):
            raise RegistrationError("invalid_redirect_uri", "Administrator must allow this exact connector callback")
        self.put("client", client_info.client_id, client_info.model_dump(mode="json"), 365*86400)

    async def authorize(self, client, params):
        if params.resource != RESOURCE:
            raise AuthorizeError("invalid_target", "Use the configured X MCP resource URL")
        if callback_key(params.redirect_uri) not in self.store.setting("callbacks", []):
            raise AuthorizeError("unauthorized_client", "Callback is not allowed")
        if set(params.scopes or DEFAULT_SCOPES) - set(SCOPES):
            raise AuthorizeError("invalid_scope", "Unknown X MCP scope")
        pending = secrets.token_urlsafe(32)
        self.put("pending", pending, {"client": client.client_id, "params": params.model_dump(mode="json")}, 300)
        return ISSUER + "/consent?request=" + pending

    async def load_authorization_code(self, client, authorization_code):
        data = self.get("code", authorization_code)
        if not data or data["client_id"] != client.client_id:
            return None
        return AuthorizationCode(code=authorization_code, **{k:v for k,v in data.items() if k != "accounts"})

    def issue(self, client_id, scopes, accounts):
        if not scopes or set(scopes) - set(SCOPES):
            raise ValueError("A known scope is required")
        if not set(scopes).intersection(ACCOUNT_SCOPES):
            accounts = []
        self.store.validate_grant_accounts(scopes, accounts)
        access, refresh, grant = (secrets.token_urlsafe(48) for _ in range(3))
        now = int(time.time())
        self.put("access", access, dict(client_id=client_id, scopes=scopes, accounts=accounts, expires_at=now+3600,
                                        resource=RESOURCE, subject="owner", grant=grant), 3600)
        self.put("refresh", refresh, dict(client_id=client_id, scopes=scopes, accounts=accounts, expires_at=now+30*86400,
                                          subject="owner", grant=grant), 30*86400)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=3600, refresh_token=refresh, scope=" ".join(scopes))

    async def exchange_authorization_code(self, client, authorization_code):
        data = self.get("code", authorization_code.code, consume=True)
        if not data or data["client_id"] != client.client_id:
            raise TokenError("invalid_grant", "Code expired or already consumed")
        return self.issue(client.client_id, data["scopes"], data["accounts"])

    async def load_refresh_token(self, client, refresh_token):
        data = self.get("refresh", refresh_token)
        if not data or data["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, **{k:v for k,v in data.items() if k not in {"grant", "accounts"}})

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        data = self.get("refresh", refresh_token.token, consume=True)
        if not data or data["client_id"] != client.client_id or set(scopes) - set(data["scopes"]):
            raise TokenError("invalid_grant", "Invalid refresh grant")
        self.revoke_grant(data["grant"])
        return self.issue(client.client_id, scopes, data["accounts"])

    async def load_access_token(self, token):
        data = self.get("access", token)
        if not data or data["resource"] != RESOURCE:
            return None
        return AccessToken(token=token, **{k:v for k,v in data.items() if k not in {"grant", "accounts"}})

    def principal(self, token):
        data = self.get("access", token)
        if data and data["resource"] == RESOURCE:
            return Principal("owner", frozenset(data["scopes"]), frozenset(data["accounts"]))

    def revoke_grant(self, grant):
        with self.db:
            for kind, key, value in self.db.execute("SELECT kind,key,value FROM records WHERE kind IN ('access','refresh')").fetchall():
                if json.loads(value)["grant"] == grant:
                    self.db.execute("DELETE FROM records WHERE kind=? AND key=?", (kind, key))

    async def revoke_token(self, token):
        data = self.get("access" if isinstance(token, AccessToken) else "refresh", token.token)
        if data:
            self.revoke_grant(data["grant"])

    async def consent(self, request):
        headers = self.headers()
        if request.method == "GET":
            pending = request.query_params.get("request", "")
            data = self.get("pending", pending)
            if not data:
                return HTMLResponse("Expired request. Reconnect your publisher.", status_code=400, headers=headers)
            nonce = secrets.token_urlsafe(32)
            data["nonce"] = self.digest(nonce)
            self.put("pending", pending, data, 300)
            requested = set(data["params"].get("scopes") or DEFAULT_SCOPES)
            choices = ""
            if requested.intersection(X_ACCOUNT_SCOPES):
                accounts = self.store.db.execute("SELECT id,username FROM accounts WHERE active=1")
                choices += "".join('<label><input type="checkbox" name="accounts" value="'
                                   +html.escape(a['id'],quote=True)+'"> X @'+html.escape(a['username'])
                                   +'</label><br>' for a in accounts)
            if requested.intersection(BUFFER_SCOPES):
                choices += "".join('<label><input type="checkbox" name="accounts" value="'
                                   +html.escape(channel['account_id'],quote=True)+'"> Buffer X '
                                   +html.escape(channel['display_name'] or channel['handle'] or channel['channel_id'])
                                   +('</label><br>') for channel in self.store.buffer_channels())
            scope_labels = ", ".join(html.escape(scope) for scope in sorted(requested))
            account_notice = ("Select accounts for the requested account permissions." if requested.intersection(ACCOUNT_SCOPES)
                              else "Public reading needs no connected X account.")
            sensitive = ''.join(
                '<label><input type="checkbox" name="approve_scope" value="'+scope+'"> '
                +html.escape(ACCOUNT_PERMISSIONS[scope])+' ('+scope+')</label>'
                for scope in sorted(requested.intersection(EXPLICIT_CONSENT_SCOPES)))
            if sensitive:
                sensitive = '<fieldset><legend>Explicit approval required</legend>'+sensitive+'</fieldset>'
            response = HTMLResponse('''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Authorize X MCP</title><style>body{font:18px system-ui;max-width:600px;margin:50px auto;padding:24px}label{display:block;margin:12px 0}input[type=password],button{padding:12px}</style><h1>Authorize X MCP</h1><p>'''+account_notice+''' Your connected credentials stay on this server. Public mirror posts are unverified and do not authorize publishing or deletion.</p><p>Requested permissions: '''+scope_labels+'''</p><form method="post" action="'''+PREFIX+'''/oauth/consent"><input type="hidden" name="request" value="'''+html.escape(pending,quote=True)+'''">'''+choices+sensitive+'''<label>Owner key <input type="password" name="key" autocomplete="off" required maxlength="256"></label><button>Authorize selected permissions</button></form></html>''', headers=headers)
            response.set_cookie(COOKIE, nonce, secure=True, httponly=True, samesite="lax", path=PREFIX+"/oauth/consent", max_age=300)
            return response
        if request.headers.get("origin") != ORIGIN:
            return JSONResponse({"error": "invalid_origin"}, status_code=403, headers=headers)
        body = await request.body()
        if len(body) > 8192:
            return JSONResponse({"error": "too_large"}, status_code=413, headers=headers)
        form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        pending = form.get("request", [""])[0]
        data = self.get("pending", pending, consume=True)
        nonce = request.cookies.get(COOKIE, "")
        if not data or not nonce or not hmac.compare_digest(data.get("nonce", ""), self.digest(nonce)):
            return JSONResponse({"error": "invalid_consent"}, status_code=403, headers=headers)
        supplied = form.get("key", [""])[0]
        if not hmac.compare_digest(supplied.encode(), self.owner_key.encode()):
            return JSONResponse({"error": "unauthorized"}, status_code=403, headers=headers)
        params = AuthorizationParams.model_validate(data["params"])
        requested = set(params.scopes or DEFAULT_SCOPES)
        approved = set(form.get("approve_scope", []))
        if approved - requested.intersection(EXPLICIT_CONSENT_SCOPES):
            return JSONResponse({"error": "invalid_scope"}, status_code=400, headers=headers)
        granted_scopes = sorted((requested - EXPLICIT_CONSENT_SCOPES) | approved)
        if not granted_scopes:
            return JSONResponse({"error": "select_permission"}, status_code=400, headers=headers)
        accounts = sorted(set(form.get("accounts", [])))
        if set(granted_scopes).intersection(ACCOUNT_SCOPES) and not accounts:
            return JSONResponse({"error": "select_active_accounts"}, status_code=400, headers=headers)
        try:
            self.store.validate_grant_accounts(granted_scopes, accounts)
        except (ValueError, Problem):
            return JSONResponse({"error": "select_active_accounts"}, status_code=400, headers=headers)
        if not set(granted_scopes).intersection(ACCOUNT_SCOPES):
            accounts = []
        code = secrets.token_urlsafe(48)
        self.put("code", code, dict(scopes=granted_scopes, accounts=accounts, expires_at=time.time()+120,
                  client_id=data["client"], code_challenge=params.code_challenge, redirect_uri=str(params.redirect_uri),
                  redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=RESOURCE, subject="owner"), 120)
        response = RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state), status_code=303, headers=headers)
        response.delete_cookie(COOKIE, path=PREFIX+"/oauth/consent", secure=True, httponly=True, samesite="lax")
        return response


def oauth_routes(provider):
    routes = create_auth_routes(provider, AnyHttpUrl(ISSUER),
        client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=SCOPES, default_scopes=DEFAULT_SCOPES),
        revocation_options=RevocationOptions(enabled=True))
    out = [Route(PREFIX+"/oauth"+r.path, r.endpoint, methods=list(r.methods)) for r in routes]
    for r in routes:
        if r.path == "/.well-known/oauth-authorization-server":
            out.append(Route("/.well-known/oauth-authorization-server"+PREFIX+"/oauth", r.endpoint, methods=list(r.methods)))
    out.extend(create_protected_resource_routes(AnyHttpUrl(RESOURCE), [AnyHttpUrl(ISSUER)], SCOPES, resource_name="X MCP"))
    out.append(Route(PREFIX+"/oauth/consent", provider.consent, methods=["GET", "POST"]))
    return out
