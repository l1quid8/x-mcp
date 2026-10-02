"""Owner-only web account settings and legacy disposable-browser routes."""
import asyncio
from datetime import datetime, timezone
import hmac
import html
import logging
import os
from pathlib import Path
import re
import secrets
import time

import aiohttp
import httpx
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from .auth import callback_key
from . import connect_ui as ui
from .core import ORIGIN, PREFIX, Problem
from .pairing import verify_and_store

BASE = PREFIX + "/connect"
CLIENT_SETTINGS = BASE + "/client-settings"
BUFFER_SETTINGS = BASE + "/buffer"
BUFFER_FALLBACK_SETTINGS = BUFFER_SETTINGS + "/fallback"
BUFFER_REFRESH = BUFFER_SETTINGS + "/refresh"
COOKIE = "__Secure-xmcp-connect"
WORKER = os.environ.get("X_MCP_BROWSER_WORKER_URL", "").rstrip("/")
VIEW = os.environ.get("X_MCP_BROWSER_VIEW_URL", "").rstrip("/")
TOKEN = os.environ.get("X_MCP_BROWSER_WORKER_TOKEN", "")
if not TOKEN and os.environ.get("CREDENTIALS_DIRECTORY"):
    credential = Path(os.environ["CREDENTIALS_DIRECTORY"]) / "browser-worker-token"
    if credential.is_file():
        TOKEN = credential.read_text().strip()
COOKIE_NAMES = {"auth_token", "ct0", "twid", "kdt", "att", "lang"}


class BrowserConnect:
    def __init__(self, oauth, store, backend_factory, account_locks):
        self.oauth, self.store = oauth, store
        self.backend_factory, self.account_locks = backend_factory, account_locks
        self.lock = asyncio.Lock()
        self.active = None

    def headers(self):
        return {"Cache-Control": "no-store", "Referrer-Policy": "strict-origin",
                "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-src 'self'; form-action 'self'; frame-ancestors 'none'"}

    def login(self, request):
        value = request.cookies.get(COOKIE, "")
        return value if self.oauth.get("connect-login", value) else None

    def configured(self):
        return WORKER.startswith("http://") and VIEW.startswith("http://") and len(TOKEN) >= 32

    async def form(self, request):
        body = await request.body()
        if len(body) > 4096:
            raise ValueError("Form too large")
        from urllib.parse import parse_qs
        return {k: v[0] for k, v in parse_qs(body.decode(), keep_blank_values=True).items() if len(v) == 1}

    async def worker(self, action, session):
        async with httpx.AsyncClient(timeout=5 if action == "reset" else 70,
                                     follow_redirects=False, trust_env=False) as client:
            return await client.post(WORKER + "/" + action, json={"session": session},
                                     headers={"Authorization": "Bearer " + TOKEN})

    async def recover(self):
        self.active = None
        if self.configured():
            try:
                await self.worker("reset", "")
            except httpx.HTTPError:
                pass

    def direct_accounts(self):
        return list(self.store.db.execute(
            "SELECT id,username,checked FROM accounts WHERE active=1 ORDER BY username"))

    @staticmethod
    def checked_label(checked):
        if not checked:
            return "Verification date unavailable"
        return "Identity last verified " + datetime.fromtimestamp(
            checked, timezone.utc).strftime("%b %d, %Y at %H:%M UTC")

    def buffer_checked_label(self):
        checked = self.store.setting("buffer_channels_verified_at")
        if not checked:
            return "Refresh channels to check the current Buffer connection."
        return "Channels last checked " + datetime.fromtimestamp(
            checked, timezone.utc).strftime("%b %d, %Y at %H:%M UTC")

    @staticmethod
    def channel_label(channel):
        name = channel.get("display_name") or ""
        handle = (channel.get("handle") or "").lstrip("@")
        if name and handle and name.lower().lstrip("@") != handle.lower():
            return ui.escape(name) + ' <span class="muted small">@' + ui.escape(handle) + "</span>"
        return ui.escape("@" + handle if handle else name or "X account")

    def channel_list(self, channels, *, accounts=()):
        if not channels:
            return '<p>No connected X channels are saved yet.</p>'
        direct_ids = {row["id"] for row in accounts}
        items = []
        for channel in channels:
            matched = channel.get("x_account_id") in direct_ids
            suffix = ui.badge("Direct backup saved", "success") if matched else ""
            items.append('<li><span class="account-name">' + self.channel_label(channel)
                         + "</span>" + suffix + "</li>")
        return '<ul class="account-list">' + "".join(items) + "</ul>"

    def direct_list(self, accounts):
        if not accounts:
            return '<p>No direct X session is saved.</p>'
        return '<ul class="account-list">' + "".join(
            '<li><span><span class="account-name">@' + ui.escape(row["username"])
            + '</span><br><span class="muted small">' + ui.escape(self.checked_label(row["checked"]))
            + '</span></span>' + ui.badge("Session saved", "success") + "</li>"
            for row in accounts) + "</ul>"

    def fallback_form(self, csrf):
        checked = " checked" if self.store.buffer_direct_fallback_enabled() else ""
        return ('<form class="stack" method="post" action="' + BUFFER_FALLBACK_SETTINGS + '">'
                + ui.hidden("csrf", csrf)
                + '<div class="checkbox"><input id="direct-fallback" type="checkbox" name="enabled" value="1"'
                + checked + '><label for="direct-fallback">Use a matching direct X session if Buffer reaches its API limit</label></div>'
                '<p class="hint">Immediate posts only. The X account must match and your MCP client must have permission to use both connections.</p>'
                '<button class="button button--primary" type="submit">Save backup setting</button></form>')

    def page_document(self, request, *, error=None):
        login = self.login(request)
        if not login:
            notice = ui.alert("Could not unlock settings", error, "danger") if error else ""
            content = ui.card(
                "Unlock account settings",
                '<p>Enter the owner key for this self-hosted server to see your connections.</p>'
                + ui.owner_key_form(), badge_html=ui.badge("Owner access", "info"))
            return ui.page("X account setup", "Manage the X accounts your MCP server can use.",
                           '<div class="grid grid--one">' + content + "</div>", notice_html=notice)
        csrf = self.oauth.get("connect-login", login)["csrf"]
        channels = self.store.buffer_channels()
        accounts = self.direct_accounts()
        fallback = self.store.buffer_direct_fallback_enabled()
        direct_ids = {a["id"] for a in accounts}
        matching = any(c.get("x_account_id") in direct_ids for c in channels)
        if channels:
            buffer_intro = ('<p>These X channels are saved from Buffer.</p>'
                            + self.channel_list(channels, accounts=accounts)
                            + '<p class="hint">' + ui.escape(self.buffer_checked_label()) + '</p>')
            buffer_badge = ui.badge("Channels saved", "success")
        else:
            buffer_intro = '<p>Connect your X account to Buffer in your own browser, then add your Buffer API key here.</p>'
            buffer_badge = ui.badge("Set up Buffer", "warning")
        buffer_card = ui.card(
            "Buffer publishing", buffer_intro, badge_html=buffer_badge,
            actions_html='<a class="button button--primary" href="' + BUFFER_SETTINGS
                         + '">Manage Buffer</a>')
        direct_intro = self.direct_list(accounts)
        if accounts:
            direct_intro += ('<p class="hint">A saved session may expire or be rejected by X. '
                             'The extension is needed when you connect or reconnect an account, not while publishing.</p>')
        else:
            direct_intro += ('<p class="hint">For direct publishing or a Buffer quota backup, connect a session '
                             'from the X account signed in on your local computer.</p>')
        direct_intro += ('<p><a href="https://github.com/l1quid8/x-mcp/blob/main/browser-extension/README.md">'
                         'How to connect with the browser extension</a></p>')
        if fallback and matching:
            direct_badge = ui.badge("Backup enabled", "success")
        elif fallback:
            direct_badge = ui.badge("Needs matching account", "warning")
        else:
            direct_badge = ui.badge("Backup off", "neutral")
        direct_card = ui.card("Direct X session", direct_intro, badge_html=direct_badge,
                              actions_html=self.fallback_form(csrf))
        notice = ui.alert("Could not unlock settings", error, "danger") if error else ""
        return ui.page("X account setup", "Manage publishing connections on your self-hosted server.",
                       '<div class="grid">' + buffer_card + direct_card + "</div>",
                       signed_in=True, notice_html=notice)

    async def page(self, request):
        return HTMLResponse(self.page_document(request), headers=self.headers())

    def buffer_document(self, request, *, error=None, open_key=False):
        login = self.login(request)
        if not login:
            notice = ui.alert("Could not unlock settings", error, "danger") if error else ""
            content = ui.card("Unlock Buffer settings",
                              '<p>Enter your server owner key to manage your Buffer connection.</p>'
                              + ui.owner_key_form(return_to="buffer"))
            return ui.page("Buffer connection", "Connect X through Buffer and manage its channels.",
                           '<div class="grid grid--one">' + content + "</div>", active="buffer",
                           notice_html=notice)
        csrf = self.oauth.get("connect-login", login)["csrf"]
        channels = self.store.buffer_channels()
        key_saved = bool(self.store.buffer_key())
        accounts = self.direct_accounts()
        saved = request.query_params.get("saved")
        if error:
            notice = ui.alert("Buffer connection needs attention", error, "danger")
        elif saved == "key":
            notice = ui.alert("Buffer connected", "Your X channels were verified and saved.", "success")
        elif saved == "refresh":
            notice = ui.alert("Channels refreshed", "Your saved X channels now match Buffer's current list.", "success")
        elif saved == "empty":
            notice = ui.alert("No X channels found", "Buffer did not return a connected X channel. Check its channel settings and refresh again.", "warning")
        elif saved == "backup":
            notice = ui.alert("Backup setting saved", "The new setting will apply to future immediate posts.", "success")
        else:
            notice = ""
        key_form = ('<form class="stack" method="post" action="' + BUFFER_SETTINGS + '">'
                    + ui.hidden("csrf", csrf)
                    + '<div class="field"><label for="buffer-key">Buffer API key</label>'
                      '<input id="buffer-key" type="password" name="api_key" required maxlength="2048" autocomplete="off" spellcheck="false">'
                      '<p class="field__help">Saved encrypted on this server. It is never sent to MCP clients.</p></div>'
                      '<button class="button button--primary" type="submit">Verify and save key</button></form>')
        instructions = ('<ol class="steps"><li><a href="https://account.buffer.com/channels">Connect your X account in Buffer</a> '
                        'using your own browser.</li><li><a href="https://publish.buffer.com/settings/api">Create a Buffer API key</a> '
                        'with account read, posts read, and posts write permissions.</li><li>Paste the key below. '
                        'Your server checks and saves its available X channels.</li></ol>')
        if key_saved:
            setup = (instructions + ('<details open>' if open_key else '<details>')
                     + '<summary>Replace API key</summary>' + key_form + '</details>')
        else:
            setup = instructions + key_form
        setup_card = ui.card("Buffer API key", setup,
                             badge_html=ui.badge("Key saved", "success") if key_saved else ui.badge("Add key", "warning"))
        if channels:
            refresh_form = ('<form method="post" action="' + BUFFER_REFRESH + '">'
                            + ui.hidden("csrf", csrf)
                            + '<button type="submit">Refresh channels</button></form>')
            channel_body = ('<p>These are the X channels saved with your Buffer key.</p>'
                            + self.channel_list(channels, accounts=accounts)
                            + '<p class="hint">' + ui.escape(self.buffer_checked_label()) + '</p>')
            channel_badge = ui.badge(str(len(channels)) + " saved", "success")
        else:
            refresh_form = ('<form method="post" action="' + BUFFER_REFRESH + '">'
                            + ui.hidden("csrf", csrf)
                            + '<button type="submit">Refresh channels</button></form>') if key_saved else ""
            channel_body = '<p>No connected X channels are saved yet. Connect one in Buffer, then refresh here.</p>'
            channel_badge = ui.badge("No channels", "warning")
        channels_card = ui.card("X channels", channel_body, badge_html=channel_badge,
                                actions_html=refresh_form)
        direct = self.direct_list(accounts)
        backup_body = ('<p>For an immediate post, a definite Buffer API quota rejection can use a saved direct X session '
                       'for the same verified X account. Other errors will stop to avoid duplicate posts.</p>'
                       + direct
                       + '<p><a href="https://github.com/l1quid8/x-mcp/blob/main/browser-extension/README.md">'
                         'Connect or reconnect a direct X session from your local browser</a></p>'
                       + self.fallback_form(csrf))
        matched = any(c.get("x_account_id") in {a["id"] for a in accounts} for c in channels)
        if self.store.buffer_direct_fallback_enabled() and matched:
            backup_badge = ui.badge("On", "success")
        elif self.store.buffer_direct_fallback_enabled():
            backup_badge = ui.badge("Needs matching account", "warning")
        else:
            backup_badge = ui.badge("Off", "neutral")
        backup_card = ui.card("Direct X backup", backup_body,
                              badge_html=backup_badge)
        return ui.page("Buffer connection", "Manage your key, X channels, and optional publishing backup.",
                       '<div class="grid">' + setup_card + channels_card + '</div>'
                       '<div class="grid grid--one section-heading">' + backup_card + '</div>',
                       active="buffer", signed_in=True, notice_html=notice)

    async def buffer_page(self, request, *, error=None, status_code=200, open_key=False):
        return HTMLResponse(self.buffer_document(request, error=error, open_key=open_key), status_code=status_code,
                            headers=self.headers())

    @staticmethod
    def usable_buffer_channels(found):
        if not isinstance(found, list):
            raise ValueError("Invalid Buffer channel list")
        channels = []
        for channel in found:
            if not isinstance(channel, dict):
                raise ValueError("Invalid Buffer channel")
            if (channel.get("service") not in {"twitter", "x"}
                    or channel.get("isDisconnected") or channel.get("isLocked")):
                continue
            channel_id = channel.get("id")
            if not isinstance(channel_id, str) or not channel_id:
                raise ValueError("Invalid Buffer channel")
            saved = {"account_id": "buffer:" + channel_id, "channel_id": channel_id,
                     "display_name": channel.get("name") or "", "handle": channel.get("username") or ""}
            if channel.get("x_account_id"):
                saved["x_account_id"] = channel["x_account_id"]
            channels.append(saved)
        return channels

    async def list_buffer_channels(self, key):
        from .buffer_api import BufferAPI
        api = BufferAPI(key)
        try:
            return self.store.validate_buffer_channels(
                self.usable_buffer_channels(await api.list_channels()))
        finally:
            await api.close()

    async def buffer_save(self, request):
        login = self.login(request)
        if not login or request.headers.get("origin") != ORIGIN:
            return JSONResponse({"error": "unauthorized"}, status_code=403, headers=self.headers())
        try:
            form = await self.form(request)
        except ValueError:
            return JSONResponse({"error": "invalid_request"}, status_code=400, headers=self.headers())
        data = self.oauth.get("connect-login", login)
        if not hmac.compare_digest(form.get("csrf", ""), data["csrf"]):
            return JSONResponse({"error": "invalid_csrf"}, status_code=403, headers=self.headers())
        key = form.get("api_key", "").strip()
        if not 16 <= len(key) <= 2048 or any(c.isspace() for c in key):
            return await self.buffer_page(request, error="Check the API key format and try again.",
                                          status_code=400, open_key=True)
        try:
            channels = await self.list_buffer_channels(key)
        except Exception:
            return await self.buffer_page(request, error="Buffer could not verify this key or list its X channels. Check the key permissions and try again.",
                                          status_code=400, open_key=True)
        if not channels:
            return await self.buffer_page(request, error="Buffer returned no connected X channels for this key. Connect X in Buffer first, then try again.",
                                          status_code=400, open_key=True)
        self.store.save_buffer_key(key)
        self.store.save_buffer_channels(channels)
        self.store.set_setting("buffer_channels_verified_at", time.time())
        return RedirectResponse(BUFFER_SETTINGS + "?saved=key", status_code=303, headers=self.headers())

    async def buffer_refresh(self, request):
        login = self.login(request)
        if not login or request.headers.get("origin") != ORIGIN:
            return JSONResponse({"error": "unauthorized"}, status_code=403, headers=self.headers())
        try:
            form = await self.form(request)
        except ValueError:
            return JSONResponse({"error": "invalid_request"}, status_code=400, headers=self.headers())
        data = self.oauth.get("connect-login", login)
        if not hmac.compare_digest(form.get("csrf", ""), data["csrf"]):
            return JSONResponse({"error": "invalid_csrf"}, status_code=403, headers=self.headers())
        key = self.store.buffer_key()
        if not key:
            return await self.buffer_page(request, error="Add a Buffer API key before refreshing channels.", status_code=400)
        try:
            channels = await self.list_buffer_channels(key)
            self.store.save_buffer_channels(channels)
            self.store.set_setting("buffer_channels_verified_at", time.time())
        except Exception:
            return await self.buffer_page(request, error="Could not refresh channels from Buffer. Your saved channels were not changed. Try again later.", status_code=502)
        return RedirectResponse(BUFFER_SETTINGS + ("?saved=refresh" if channels else "?saved=empty"),
                                status_code=303, headers=self.headers())

    async def buffer_fallback_save(self, request):
        login = self.login(request)
        if not login or request.headers.get("origin") != ORIGIN:
            return JSONResponse({"error": "unauthorized"}, status_code=403, headers=self.headers())
        try:
            form = await self.form(request)
        except ValueError:
            return JSONResponse({"error": "invalid_request"}, status_code=400, headers=self.headers())
        data = self.oauth.get("connect-login", login)
        if not hmac.compare_digest(form.get("csrf", ""), data["csrf"]):
            return JSONResponse({"error": "invalid_csrf"}, status_code=403, headers=self.headers())
        if form.get("enabled", "") not in {"", "1"}:
            return JSONResponse({"error": "invalid_request"}, status_code=400, headers=self.headers())
        self.store.set_buffer_direct_fallback_enabled(form.get("enabled") == "1")
        return RedirectResponse(BUFFER_SETTINGS + "?saved=backup", status_code=303, headers=self.headers())

    def client_document(self, request, *, error=None, success=False):
        login = self.login(request)
        if not login:
            content = ui.card("Unlock MCP client settings",
                              '<p>Enter your owner key to manage callback URLs for MCP clients.</p>'
                              + ui.owner_key_form(return_to="client-settings"))
        else:
            csrf = self.oauth.get("connect-login", login)["csrf"]
            content = ui.card(
                "Allow an MCP client callback",
                '<p>Use this only if your MCP client says its callback URL is blocked. '
                'This setting returns you to the client after you approve access; it does not connect an X account.</p>'
                '<form class="stack" method="post" action="' + BASE + '/callback">'
                + ui.hidden("csrf", csrf)
                + '<div class="field"><label for="callback-url">Exact client callback URL</label>'
                  '<input id="callback-url" type="url" name="callback" required></div>'
                  '<button class="button button--primary" type="submit">Allow callback</button></form>')
        if error:
            notice = ui.alert("Callback was not saved", error, "danger")
        elif success:
            notice = ui.alert("Callback allowed", "Retry the MCP client connection, then approve its accounts and permissions.", "success")
        else:
            notice = ""
        return ui.page("MCP client settings", "Approve a callback URL only when your MCP client asks for one.",
                       '<div class="grid grid--one">' + content + "</div>", active="clients",
                       signed_in=bool(login), notice_html=notice)

    async def client_settings(self, request):
        return HTMLResponse(self.client_document(request), headers=self.headers())

    async def authorize(self, request):
        if request.headers.get("origin") != ORIGIN:
            return JSONResponse({"error": "invalid_origin"}, status_code=403, headers=self.headers())
        try:
            form = await self.form(request)
            supplied = form.get("key", "")
        except ValueError:
            supplied = ""
            form = {}
        if not hmac.compare_digest(supplied.encode(), self.oauth.owner_key.encode()):
            destination = form.get("return_to", "")
            if destination == "buffer":
                body = self.buffer_document(request, error="That owner key was not accepted. Check it and try again.")
            elif destination == "client-settings":
                body = self.client_document(request, error="That owner key was not accepted. Check it and try again.")
            else:
                body = self.page_document(request, error="That owner key was not accepted. Check it and try again.")
            return HTMLResponse(body, status_code=403, headers=self.headers())
        login, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.oauth.put("connect-login", login, {"csrf": csrf}, 900)
        destination = (CLIENT_SETTINGS if form.get("return_to") == "client-settings" else
                       BUFFER_SETTINGS if form.get("return_to") == "buffer" else BASE)
        response = RedirectResponse(destination, status_code=303, headers=self.headers())
        response.set_cookie(COOKIE, login, secure=True, httponly=True, samesite="strict", path=BASE, max_age=900)
        return response

    async def action(self, request):
        login = self.login(request)
        if not login or request.headers.get("origin") != ORIGIN:
            return JSONResponse({"error": "unauthorized"}, status_code=403, headers=self.headers())
        try:
            form = await self.form(request)
        except ValueError:
            return JSONResponse({"error": "invalid_request"}, status_code=400, headers=self.headers())
        data = self.oauth.get("connect-login", login)
        if not hmac.compare_digest(form.get("csrf", ""), data["csrf"]):
            return JSONResponse({"error": "invalid_csrf"}, status_code=403, headers=self.headers())
        action = request.path_params["action"]
        if action not in {"start", "finish", "cancel", "reset", "callback"}:
            return Response(status_code=404)
        if action == "callback":
            callback = callback_key(form.get("callback", ""))
            if callback is None:
                return HTMLResponse(self.client_document(request, error="Use the exact ChatGPT HTTPS callback or Codex 127.0.0.1 callback URL shown by your client."), status_code=400, headers=self.headers())
            allowed = self.store.setting("callbacks", [])
            self.store.set_setting("callbacks", sorted(set(allowed + [callback])))
            return HTMLResponse(self.client_document(request, success=True), headers=self.headers())
        async with self.lock:
            if self.active and time.monotonic() >= self.active["expires"]:
                self.active = None
            if action == "reset":
                if not self.active:
                    return RedirectResponse(BASE, status_code=303, headers=self.headers())
                try:
                    result = await self.worker("reset", "")
                except httpx.HTTPError:
                    return HTMLResponse("Could not end the previous sign-in. Please try again.", status_code=503, headers=self.headers())
                if result.status_code != 200:
                    return HTMLResponse("Could not end the previous sign-in. Please try again.", status_code=503, headers=self.headers())
                self.active = None
                return RedirectResponse(BASE, status_code=303, headers=self.headers())
            if action == "start":
                if not self.configured():
                    return JSONResponse({"error": "browser_unavailable"}, status_code=503, headers=self.headers())
                if self.active:
                    return RedirectResponse(BASE, status_code=303, headers=self.headers())
                account = form.get("account", "")
                if account != "new":
                    try:
                        self.store.account(account)
                    except Problem:
                        return JSONResponse({"error": "unknown_account"}, status_code=400, headers=self.headers())
                session = secrets.token_urlsafe(32)
                try:
                    result = await self.worker("start", session)
                except httpx.HTTPError:
                    return JSONResponse({"error": "browser_unavailable"}, status_code=503, headers=self.headers())
                if result.status_code != 200:
                    return JSONResponse({"error": "browser_unavailable"}, status_code=503, headers=self.headers())
                self.active = {"login": login, "session": session, "account": account,
                               "expires": time.monotonic() + 900}
                return RedirectResponse(BASE, status_code=303, headers=self.headers())
            if not self.active or not hmac.compare_digest(self.active["login"], login):
                return JSONResponse({"error": "session_unavailable"}, status_code=404, headers=self.headers())
            active = self.active
            self.active = None
            try:
                result = await self.worker("stop" if action == "cancel" else "finish", active["session"])
                if action == "cancel":
                    return RedirectResponse(BASE, status_code=303, headers=self.headers())
                if result.status_code != 200:
                    return HTMLResponse("X login was not complete. Start a new connection.", status_code=400, headers=self.headers())
                cookies = result.json().get("cookies", {})
                identity = await self.import_session(cookies, active["account"])
                return HTMLResponse("<h1>Connected @"+html.escape(identity["username"])+"</h1><p>Your X session is stored on this server. Return to your MCP client to grant this account access.</p>", headers=self.headers())
            except Exception as exc:
                logging.getLogger("x_publisher.connect").warning("Browser connection failed: exception=%s", re.sub(r"[^A-Za-z0-9_]", "", type(exc).__name__)[:64])
                return HTMLResponse("Connection could not be verified. Start a new connection.", status_code=400, headers=self.headers())

    async def import_session(self, cookies, expected):
        if (not isinstance(cookies, dict) or set(cookies) - COOKIE_NAMES
                or not all(isinstance(v, str) and 0 < len(v) <= 4096 for v in cookies.values())
                or not cookies.get("auth_token") or not cookies.get("ct0")):
            raise ValueError("Invalid session")
        return await verify_and_store(self.store, self.backend_factory, self.account_locks, cookies,
                                      expected_id=None if expected == "new" else expected)

    async def view(self, request):
        login = self.login(request)
        if (not self.active or not login or time.monotonic() >= self.active["expires"]
                or not hmac.compare_digest(self.active["login"], login)):
            return Response(status_code=403)
        path = request.path_params["path"]
        if not re.fullmatch(r"[A-Za-z0-9_./-]{1,160}", path) or ".." in path:
            return Response(status_code=404)
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
                upstream = await client.get(VIEW + "/" + path)
            if upstream.status_code != 200 or len(upstream.content) > 4_000_000:
                return Response(status_code=404)
            return Response(upstream.content, media_type=upstream.headers.get("content-type", "application/octet-stream"),
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                     "Content-Security-Policy": "default-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; connect-src 'self' wss:; frame-ancestors 'self'"})
        except httpx.HTTPError:
            return Response(status_code=503)

    async def websocket(self, websocket):
        login = websocket.cookies.get(COOKIE, "")
        if (websocket.headers.get("origin") != ORIGIN or not self.oauth.get("connect-login", login)
                or not self.active or time.monotonic() >= self.active["expires"]
                or not hmac.compare_digest(self.active["login"], login)):
            await websocket.close(code=1008)
            return
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(VIEW.replace("http://", "ws://") + "/websockify", heartbeat=20) as remote:
                    await websocket.accept()
                    async def to_remote():
                        while True:
                            message = await websocket.receive()
                            if message["type"] == "websocket.disconnect":
                                break
                            if message.get("bytes") is not None:
                                await remote.send_bytes(message["bytes"])
                            elif message.get("text") is not None:
                                await remote.send_str(message["text"])
                    async def to_client():
                        async for message in remote:
                            if message.type == aiohttp.WSMsgType.BINARY:
                                await websocket.send_bytes(message.data)
                            elif message.type == aiohttp.WSMsgType.TEXT:
                                await websocket.send_text(message.data)
                    tasks = [asyncio.create_task(to_remote()), asyncio.create_task(to_client())]
                    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (aiohttp.ClientError, WebSocketDisconnect):
            pass
        finally:
            try:
                await websocket.close()
            except RuntimeError:
                pass


def browser_connect_routes(flow):
    return [Route(BASE, flow.page, methods=["GET"]),
            Route(CLIENT_SETTINGS, flow.client_settings, methods=["GET"]),
            Route(BUFFER_SETTINGS, flow.buffer_page, methods=["GET"]),
            Route(BUFFER_SETTINGS, flow.buffer_save, methods=["POST"]),
            Route(BUFFER_REFRESH, flow.buffer_refresh, methods=["POST"]),
            Route(BUFFER_FALLBACK_SETTINGS, flow.buffer_fallback_save, methods=["POST"]),
            Route(BASE+"/login", flow.authorize, methods=["POST"]),
            Route(BASE+"/{action}", flow.action, methods=["POST"]),
            Route(BASE+"/view/{path:path}", flow.view, methods=["GET"]),
            WebSocketRoute(BASE+"/ws", flow.websocket)]
