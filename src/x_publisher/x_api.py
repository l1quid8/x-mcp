"""Official X v2 user-context adapter. Never uses the publishing cookie backend."""
import asyncio
import base64
import hashlib
import json
import time
from urllib.parse import urlencode

import httpx

from .core import ORIGIN, PREFIX, Problem, canonical, identifier

READ_SCOPES = {"tweet.read", "users.read"}
POST_SCOPES = READ_SCOPES | {"tweet.write"}
DM_READ_SCOPES = READ_SCOPES | {"dm.read"}
DM_WRITE_SCOPES = DM_READ_SCOPES | {"dm.write"}
CALLBACK = ORIGIN + PREFIX + "/x-oauth/callback"
POST_FIELDS = "id,text,created_at,conversation_id,public_metrics,attachments,note_post"
POST_PARAMS = {"post.fields": POST_FIELDS,
               "expansions": "author_id,referenced_posts,attachments.media_keys",
               "user.fields": "id,username,name",
               "media.fields": "media_key,type,url,preview_image_url,width,height,duration_ms,alt_text"}
DM_PARAMS = {"dm_event.fields": "id,text,event_type,created_at,dm_conversation_id,attachments",
             "expansions": "sender_id,participant_ids,attachments.media_keys",
             "user.fields": "id,username,name",
             "media.fields": "media_key,type,url,preview_image_url,width,height,duration_ms,alt_text"}


class XAPIError(Problem):
    def __init__(self, code, status=0, retry_at=0, rate=None):
        message = {
            "rate_limited": "X rate-limited this request; wait until retry_at before retrying",
            "session_or_account_restricted": "Reconnect this account or resolve its restriction in X",
            "transport_error": "Could not reach X; retry this read when the connection is available",
            "transport_unknown": "The write outcome is uncertain; do not replay this request",
            "remote_unknown": "The write outcome is uncertain; do not replay this request",
        }.get(code, "X request failed; inspect status and reconnect or retry when permitted")
        super().__init__(code, message)
        self.status, self.retry_at, self.rate = status, retry_at, rate or {}


def rate_headers(headers):
    result = {}
    for source, dest in [("x-rate-limit-limit", "limit"), ("x-rate-limit-remaining", "remaining"),
                         ("x-rate-limit-reset", "reset")]:
        try:
            result[dest] = int(headers[source])
        except (KeyError, ValueError):
            pass
    return result


class XOAuth:
    def __init__(self, repository, client_factory=httpx.AsyncClient):
        self.repo = repository
        self.client_factory = client_factory
        self.locks = {}

    def config(self):
        row = self.repo.db.execute("SELECT value FROM credentials WHERE account='__app__'").fetchone()
        if not row:
            raise Problem("x_oauth_app_missing", "Configure an official X OAuth application first")
        return json.loads(self.repo.store.cipher.decrypt(row[0]))

    def save_app(self, client_id, client_secret=None):
        if not client_id or len(client_id) > 512:
            raise ValueError("Invalid official OAuth client ID")
        self.repo.save_credential("__app__", {"client_id": client_id, "client_secret": client_secret})

    def begin(self, account, with_dms=False):
        self.repo.store.account(account)
        config = self.config()
        state, verifier = identifier(), identifier() + identifier()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        scopes = POST_SCOPES | {"offline.access"} | (DM_WRITE_SCOPES if with_dms else set())
        payload = {"account": account, "verifier": verifier, "scopes": sorted(scopes)}
        with self.repo.db:
            self.repo.db.execute("DELETE FROM auth_requests WHERE expires < ?", (time.time(),))
            self.repo.db.execute("INSERT INTO auth_requests VALUES (?,?,?)", (
                hashlib.sha256(state.encode()).hexdigest(), self.repo.store.cipher.encrypt(canonical(payload).encode()), time.time()+600))
        return "https://x.com/i/oauth2/authorize?" + urlencode({"response_type": "code", "client_id": config["client_id"],
            "redirect_uri": CALLBACK, "scope": " ".join(sorted(scopes)), "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256"})

    async def exchange(self, data):
        config = self.config()
        auth = httpx.BasicAuth(config["client_id"], config["client_secret"]) if config.get("client_secret") else None
        if auth is None:
            data = {**data, "client_id": config["client_id"]}
        async with self.client_factory(timeout=20, follow_redirects=False) as client:
            try:
                response = await client.post("https://api.x.com/2/oauth2/token", data=data, auth=auth)
                response.raise_for_status()
                token = response.json()
                if not isinstance(token.get("access_token"), str) or not token["access_token"]:
                    raise ValueError()
                token["expires_at"] = time.time() + int(token.get("expires_in", 7200))
                return token
            except Exception:
                raise Problem("x_oauth_failed", "Official X authorization failed; reconnect using a new authorization request") from None

    async def complete(self, state, code):
        with self.repo.db:
            row = self.repo.db.execute("DELETE FROM auth_requests WHERE hash=? AND expires>? RETURNING payload", (
                hashlib.sha256(state.encode()).hexdigest(), time.time())).fetchone()
        if not row:
            raise Problem("invalid_x_oauth_state", "X authorization request expired or was already used")
        request = json.loads(self.repo.store.cipher.decrypt(row[0]))
        token = await self.exchange({"grant_type": "authorization_code", "code": code,
            "redirect_uri": CALLBACK, "code_verifier": request["verifier"]})
        scopes = set(token.get("scope", "").split())
        if not READ_SCOPES <= scopes or not scopes <= set(request["scopes"]):
            raise Problem("x_scope_mismatch", "X returned an unexpected permission grant")
        backend = OfficialX(token["access_token"], scopes, self.client_factory)
        identity = await backend.identity()
        if identity["id"] != request["account"]:
            raise Problem("identity_mismatch", "Authorize the exact X account selected by the administrator")
        self.repo.store.account(request["account"])
        self.repo.save_credential(request["account"], token)
        return {"account_id": request["account"], "connected": True, "scopes": sorted(scopes)}

    async def backend(self, account):
        self.repo.store.account(account)
        async with self.locks.setdefault(account, asyncio.Lock()):
            row = self.repo.db.execute("SELECT value FROM credentials WHERE account=?", (account,)).fetchone()
            if not row:
                raise Problem("official_x_not_connected", "Connect this account through official X OAuth; publishing sessions cannot authorize cleanup")
            token = json.loads(self.repo.store.cipher.decrypt(row[0]))
            if token["expires_at"] <= time.time()+60:
                if not token.get("refresh_token"):
                    raise Problem("x_oauth_expired", "Reconnect official X authorization")
                refreshed = await self.exchange({"grant_type": "refresh_token", "refresh_token": token["refresh_token"]})
                refreshed.setdefault("refresh_token", token["refresh_token"])
                refreshed.setdefault("scope", token.get("scope", ""))
                if set(refreshed["scope"].split()) - set(token.get("scope", "").split()):
                    raise Problem("x_scope_mismatch", "Unexpected permissions during refresh")
                token = refreshed
                self.repo.save_credential(account, token)
            return OfficialX(token["access_token"], set(token.get("scope", "").split()), self.client_factory)


class OfficialX:
    def __init__(self, token, scopes, client_factory=httpx.AsyncClient):
        self.token, self.scopes, self.client_factory = token, set(scopes), client_factory
        self.rate = {}

    def require(self, scopes):
        if not set(scopes) <= self.scopes:
            raise Problem("missing_x_scopes", "Reconnect X OAuth with the required endpoint scopes")

    async def request(self, method, path, params=None):
        # Fixed origin, bounded timeout, no redirects, no raw upstream bodies in receipts/errors.
        async with self.client_factory(timeout=20, follow_redirects=False) as client:
            try:
                response = await client.request(method, "https://api.x.com/2"+path, params=params,
                    headers={"Authorization": "Bearer "+self.token})
            except httpx.HTTPError:
                raise XAPIError("transport_unknown" if method == "DELETE" else "transport_error") from None
        self.rate = rate_headers(response.headers)
        if response.status_code == 429:
            try:
                retry = time.time() + max(1, int(response.headers.get("retry-after", "60")))
            except ValueError:
                retry = time.time()+60
            raise XAPIError("rate_limited", 429, max(retry, self.rate.get("reset", 0)), self.rate)
        if response.status_code >= 500:
            raise XAPIError("remote_unknown" if method == "DELETE" else "transient_x_error", response.status_code, time.time()+30)
        if not 200 <= response.status_code < 300:
            raise XAPIError("x_not_found" if response.status_code == 404 else "x_rejected", response.status_code)
        try:
            result = response.json() if response.content else {}
        except ValueError:
            raise XAPIError("remote_unknown" if method == "DELETE" else "invalid_x_response", response.status_code) from None
        if result.get("errors"):
            # Lookup field hydration failures are not proof of absence; fail closed.
            raise XAPIError("remote_unknown" if method == "DELETE" else "incomplete_x_response", response.status_code)
        return result

    async def identity(self):
        self.require(READ_SCOPES)
        return (await self.request("GET", "/users/me"))["data"]

    async def pinned(self, account):
        # This field is in the established user lookup API. Some current docs omit it;
        # rejection prevents deletion rather than silently ignoring pin protection.
        result = await self.request("GET", "/users/"+account, {"user.fields": "pinned_tweet_id"})
        return result["data"].get("pinned_tweet_id")

    async def scan(self, account, kind, cursor=None, limit=100):
        self.require(DM_READ_SCOPES if kind == "dms" else READ_SCOPES)
        params = dict(DM_PARAMS if kind == "dms" else POST_PARAMS, max_results=limit)
        if cursor:
            params["next_token" if kind == "archive_posts" else "pagination_token"] = cursor
        if kind == "archive_posts":
            # Query is server-built from the verified identity, never arbitrary
            # client text. Full-archive entitlement is separately enforced by X.
            identity = await self.identity()
            username = identity.get("username")
            import re
            if identity.get("id") != account or not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", username):
                raise Problem("identity_mismatch", "Archive scan requires the authenticated account's verified handle")
            params["query"] = "from:"+username+" -is:retweet"
            return await self.request("GET", "/tweets/search/all", params)
        if kind == "dms":
            params["event_types"] = "MessageCreate"
        return await self.request("GET", "/dm_events" if kind == "dms" else "/users/"+account+"/tweets", params)

    async def lookup(self, candidate):
        dm = candidate["content_type"] == "DM"
        self.require(DM_READ_SCOPES if dm else READ_SCOPES)
        return await self.request("GET", ("/dm_events/" if dm else "/tweets/")+candidate["content_id"], DM_PARAMS if dm else POST_PARAMS)

    async def execute(self, account, action, target):
        self.require(DM_WRITE_SCOPES if action == "DELETE_DM" else POST_SCOPES)
        path = {"DELETE_POST": "/tweets/"+target, "UNDO_REPOST": "/users/"+account+"/retweets/"+target,
                "DELETE_DM": "/dm_events/"+target}[action]
        result = await self.request("DELETE", path)
        expected = "retweeted" if action == "UNDO_REPOST" else "deleted"
        value = result.get("data", {}).get(expected)
        if value is not (False if action == "UNDO_REPOST" else True):
            raise XAPIError("remote_unknown", 200)
        return {"http_status": 200, "endpoint": path, "data": {expected: value}, "rate_limit": self.rate}
