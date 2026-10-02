"""Disposable, single-owner browser. Reachable only on the private service network."""
import asyncio
import json
import os
import secrets
import time

from playwright.async_api import async_playwright
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

SECRET = os.environ["X_MCP_BROWSER_WORKER_TOKEN"]
if len(SECRET) < 32:
    raise RuntimeError("Set a random browser worker token of at least 32 characters")


class Worker:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.playwright = None
        self.browser = None
        self.context = None
        self.session = None
        self.started = 0.0

    async def stop(self):
        for resource in (self.context, self.browser, self.playwright):
            if resource:
                try:
                    await (resource.stop() if resource is self.playwright else resource.close())
                except Exception:
                    pass
        self.context = self.browser = self.playwright = self.session = None
        self.started = 0.0

    async def expire(self):
        while True:
            await asyncio.sleep(15)
            async with self.lock:
                if self.session and time.monotonic() - self.started > 900:
                    await self.stop()

    async def request(self, request: Request):
        if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + SECRET):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if request.url.path == "/reset":
            async with self.lock:
                await self.stop()
            return JSONResponse({"state": "closed"})
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            return JSONResponse({"error": "invalid_payload"}, status_code=400)
        try:
            body = await request.body()
            if len(body) > 256:
                raise ValueError()
            data = json.loads(body)
            session = data["session"]
            if not isinstance(session, str) or len(session) != 43:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "invalid_payload"}, status_code=400)
        async with self.lock:
            if self.session and time.monotonic() - self.started > 900:
                await self.stop()
            if request.url.path == "/start":
                if self.session:
                    return JSONResponse({"error": "busy"}, status_code=409)
                try:
                    self.playwright = await async_playwright().start()
                    self.browser = await self.playwright.chromium.launch(headless=False, args=["--no-first-run", "--disable-dev-shm-usage", "--window-size=1280,800"])
                    self.context = await self.browser.new_context(accept_downloads=False, viewport={"width": 1280, "height": 720})
                    page = await self.context.new_page()
                    await page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded", timeout=60000)
                    self.session, self.started = session, time.monotonic()
                except Exception:
                    await self.stop()
                    return JSONResponse({"error": "browser_unavailable"}, status_code=503)
                return JSONResponse({"state": "ready"})
            if self.session != session:
                return JSONResponse({"error": "session_unavailable"}, status_code=404)
            if request.url.path == "/finish":
                try:
                    cookies = {c["name"]: c["value"] for c in await self.context.cookies("https://x.com")
                               if c.get("domain", "").lstrip(".") == "x.com"
                               and c["name"] in {"auth_token", "ct0", "twid", "kdt", "att", "lang"}}
                finally:
                    await self.stop()
                if not cookies.get("auth_token") or not cookies.get("ct0"):
                    return JSONResponse({"error": "login_incomplete"}, status_code=400)
                return JSONResponse({"cookies": cookies}, headers={"Cache-Control": "no-store"})
            await self.stop()
            return JSONResponse({"state": "closed"})


worker = Worker()
app = Starlette(routes=[Route("/start", worker.request, methods=["POST"]),
                        Route("/finish", worker.request, methods=["POST"]),
                        Route("/stop", worker.request, methods=["POST"]),
                        Route("/reset", worker.request, methods=["POST"]),
                        Route("/health", lambda request: JSONResponse({"ready": True}), methods=["GET"])])
@app.on_event("startup")
async def startup():
    app.state.expiry_task = asyncio.create_task(worker.expire())


@app.on_event("shutdown")
async def shutdown():
    app.state.expiry_task.cancel()
    async with worker.lock:
        await worker.stop()
