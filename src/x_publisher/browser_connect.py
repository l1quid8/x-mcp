"""Owner-only web onboarding through a disposable browser on the host."""
import asyncio
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
from .core import ORIGIN, PREFIX, Problem
from .pairing import verify_and_store

BASE = PREFIX + "/connect"
CLIENT_SETTINGS = BASE + "/client-settings"
BUFFER_SETTINGS = BASE + "/buffer"
BUFFER_FALLBACK_SETTINGS = BUFFER_SETTINGS + "/fallback"
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

    async def page(self, request):
        login = self.login(request)
        if not login:
            return HTMLResponse('''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Connect X</title><style>body{font:18px system-ui;max-width:620px;margin:50px auto;padding:24px}input,button{font:inherit;padding:12px}</style><h1>Connect X account</h1><p>For an X account already linked in Buffer, use <a href="'''+BUFFER_SETTINGS+'''">Connect through Buffer</a>. For a direct X session on your own server, continue below.</p><form method="post" action="'''+BASE+'''/login"><label>Server owner key <input type="password" name="key" required maxlength="256" autocomplete="off"></label><button>Continue</button></form></html>''', headers=self.headers())
        data = self.oauth.get("connect-login", login)
        csrf = html.escape(data["csrf"], quote=True)
        rows = list(self.store.db.execute("SELECT id,username FROM accounts WHERE active=1 ORDER BY username"))
        choices = '<option value="new">New account</option>' + ''.join(
            '<option value="'+html.escape(row["id"], quote=True)+'">Reconnect @'+html.escape(row["username"])+"</option>" for row in rows)
        if self.active and time.monotonic() >= self.active["expires"]:
            self.active = None
        active = self.active and hmac.compare_digest(self.active["login"], login)
        if not self.configured():
            content = '<p>The VPS browser is not configured on this server.</p>'
        elif active:
            content = '<p>Sign in to X below. When you see your home feed, select Finish connection.</p><iframe title="X login browser on your server" src="'+BASE+'/view/vnc.html?autoconnect=1&amp;resize=scale&amp;path='+BASE.lstrip('/')+'/ws" style="width:100%;height:650px;border:1px solid #888"></iframe>'
            content += '<form method="post" action="'+BASE+'/finish"><input type="hidden" name="csrf" value="'+csrf+'"><button>Finish connection</button></form>'
            content += '<form method="post" action="'+BASE+'/cancel"><input type="hidden" name="csrf" value="'+csrf+'"><button>Cancel</button></form>'
        elif self.active:
            content = ('<p>Another X sign-in is in progress in a different browser. '
                       'You can end that attempt and start again here. The other browser will close without saving its session.</p>'
                       '<form method="post" action="'+BASE+'/reset"><input type="hidden" name="csrf" value="'+csrf+'">'
                       '<button>End previous sign-in</button></form>')
        else:
            content = '<form method="post" action="'+BASE+'/start"><input type="hidden" name="csrf" value="'+csrf+'"><label>Account <select name="account">'+choices+'</select></label><button>Open X sign-in</button></form>'
        content += '<p><a href="'+BUFFER_SETTINGS+'">Connect through Buffer</a> using an X account already linked in Buffer.</p>'
        return HTMLResponse('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Connect X</title><style>body{font:18px system-ui;max-width:1000px;margin:24px auto;padding:24px}label,button{font:inherit;margin:12px}</style><h1>Connect X account</h1>'+content+'</html>', headers=self.headers())

    async def buffer_page(self, request):
        login = self.login(request)
        if not login:
            content = ('<p>Connect your X account in Buffer first, then enter your server owner key here.</p>'
                       '<form method="post" action="'+BASE+'/login"><input type="hidden" name="return_to" value="buffer">'
                       '<label>Server owner key <input type="password" name="key" required maxlength="256" autocomplete="off"></label>'
                       '<button>Continue</button></form>')
        else:
            csrf = html.escape(self.oauth.get("connect-login", login)["csrf"], quote=True)
            channels = self.store.buffer_channels()
            if channels:
                listed = '<ul>'+''.join('<li>'+html.escape(c["display_name"] or c["handle"] or c["channel_id"])+
                                         ' ('+html.escape(c["account_id"])+')</li>' for c in channels)+'</ul>'
            else:
                listed = '<p>No Buffer X channel is configured yet.</p>'
            checked = ' checked' if self.store.buffer_direct_fallback_enabled() else ''
            content = ('<p>Connect X to Buffer in your own browser, then <a href="https://publish.buffer.com/settings/api">create a Buffer API key</a> and paste it here. '
                       'The key is encrypted on this server and is never sent to MCP clients. '
                       'Choose only account-read, posts-read and posts-write permissions for this key.</p>'
                       +listed+
                       '<form method="post" action="'+BUFFER_SETTINGS+'"><input type="hidden" name="csrf" value="'+csrf+'">'
                       '<label>Buffer API key <input type="password" name="api_key" required maxlength="2048" autocomplete="off" style="width:100%;box-sizing:border-box"></label>'
                       '<button>Connect Buffer X channels</button></form>'
                       '<h2>Publishing backup</h2><p>When Buffer rejects a post because its API quota is exhausted, '
                       'use an existing direct X session for the same verified X account. '
                       'This applies to immediate posts only. Your MCP client must have permission to publish '
                       'through both routes, and the direct X session must still work.</p>'
                       '<form method="post" action="'+BUFFER_FALLBACK_SETTINGS+'">'
                       '<input type="hidden" name="csrf" value="'+csrf+'">'
                       '<label><input type="checkbox" name="enabled" value="1"'+checked+'>'
                       ' Use same-account direct X session after Buffer quota rejection</label>'
                       '<button>Save backup setting</button></form>')
        return HTMLResponse('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Connect through Buffer</title><style>body{font:18px system-ui;max-width:620px;margin:50px auto;padding:24px}input,button{font:inherit;padding:12px}label{display:block;margin:20px 0}</style><h1>Connect through Buffer</h1>'+content+'</html>', headers=self.headers())

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
            return HTMLResponse("Invalid Buffer API key format.", status_code=400, headers=self.headers())
        from .buffer_api import BufferAPI
        api = BufferAPI(key)
        try:
            found = await api.list_channels()
        except Exception:
            return HTMLResponse("Buffer could not verify this key or list its X channels. Check the key permissions and try again.",
                                status_code=400, headers=self.headers())
        finally:
            await api.close()
        channels = []
        for c in found:
            if (c.get("service") not in {"twitter", "x"}
                    or c.get("isDisconnected") or c.get("isLocked")):
                continue
            channel = {"account_id": "buffer:"+c["id"], "channel_id": c["id"],
                       "display_name": c.get("name") or "", "handle": c.get("username") or ""}
            if c.get("x_account_id"):
                channel["x_account_id"] = c["x_account_id"]
            channels.append(channel)
        if not channels:
            return HTMLResponse("Buffer returned no connected X channels for this key.", status_code=400, headers=self.headers())
        self.store.save_buffer_key(key)
        self.store.save_buffer_channels(channels)
        return RedirectResponse(BUFFER_SETTINGS, status_code=303, headers=self.headers())

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
        return RedirectResponse(BUFFER_SETTINGS, status_code=303, headers=self.headers())

    async def client_settings(self, request):
        login = self.login(request)
        if not login:
            content = ('<p>Enter your server owner key to manage MCP client callbacks.</p>'
                       '<form method="post" action="'+BASE+'/login"><input type="hidden" name="return_to" value="client-settings">'
                       '<label>Server owner key <input type="password" name="key" required maxlength="256" autocomplete="off"></label>'
                       '<button>Continue</button></form>')
        else:
            csrf = html.escape(self.oauth.get("connect-login", login)["csrf"], quote=True)
            content = ('<p>Use this only when Codex or ChatGPT says its callback URL is not allowed. '
                       'The callback returns you to that MCP client after you approve access; it does not connect an X account.</p>'
                       '<form method="post" action="'+BASE+'/callback"><input type="hidden" name="csrf" value="'+csrf+'">'
                       '<label>Exact client callback URL <input type="url" name="callback" required style="display:block;width:100%;box-sizing:border-box"></label>'
                       '<button>Allow callback</button></form>')
        return HTMLResponse('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MCP client settings</title><style>body{font:18px system-ui;max-width:620px;margin:50px auto;padding:24px}input,button{font:inherit;padding:12px}label{display:block;margin:20px 0}</style><h1>MCP client settings</h1>'+content+'</html>', headers=self.headers())

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
            return JSONResponse({"error": "unauthorized"}, status_code=403, headers=self.headers())
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
                return HTMLResponse("Only an exact ChatGPT HTTPS callback or Codex 127.0.0.1 callback path is supported.", status_code=400, headers=self.headers())
            allowed = self.store.setting("callbacks", [])
            self.store.set_setting("callbacks", sorted(set(allowed + [callback])))
            return HTMLResponse("<h1>Callback allowed</h1><p>Retry the MCP client connection. You will approve its accounts and permissions on the consent page.</p>", headers=self.headers())
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
            Route(BUFFER_FALLBACK_SETTINGS, flow.buffer_fallback_save, methods=["POST"]),
            Route(BASE+"/login", flow.authorize, methods=["POST"]),
            Route(BASE+"/{action}", flow.action, methods=["POST"]),
            Route(BASE+"/view/{path:path}", flow.view, methods=["GET"]),
            WebSocketRoute(BASE+"/ws", flow.websocket)]
