"""Narrow browser CORS support for the ChatGPT MCP transport."""

from starlette.datastructures import MutableHeaders
from starlette.responses import Response

from .core import ORIGIN, PREFIX


CHATGPT_ORIGIN = "https://chatgpt.com"
MCP_PATH = PREFIX + "/mcp"
MCP_METHODS = frozenset({"GET", "POST", "DELETE"})
MCP_REQUEST_HEADERS = frozenset({
    "accept", "authorization", "content-type", "last-event-id",
    "mcp-protocol-version", "mcp-session-id",
})


class ChatGPTMcpCORS:
    """Answer trusted MCP preflights and expose its HTTP auth challenge to ChatGPT."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != MCP_PATH:
            return await self.app(scope, receive, send)

        headers = dict(scope["headers"])
        origin = headers.get(b"origin", b"").decode("latin-1")
        if origin and origin not in (ORIGIN, CHATGPT_ORIGIN):
            return await Response(status_code=403)(scope, receive, send)

        if scope["method"] == "OPTIONS" and origin == CHATGPT_ORIGIN:
            method = headers.get(b"access-control-request-method", b"").decode("latin-1")
            requested = {part.strip().decode("latin-1").lower()
                         for part in headers.get(b"access-control-request-headers", b"").split(b",")
                         if part.strip()}
            if method not in MCP_METHODS or not requested <= MCP_REQUEST_HEADERS:
                return await Response(status_code=403)(scope, receive, send)
            return await Response(status_code=204, headers={
                "Access-Control-Allow-Origin": CHATGPT_ORIGIN,
                "Access-Control-Allow-Methods": "GET, POST, DELETE",
                "Access-Control-Allow-Headers": "Accept, Authorization, Content-Type, Last-Event-ID, MCP-Protocol-Version, MCP-Session-Id",
                "Access-Control-Max-Age": "300",
                "Vary": "Origin, Access-Control-Request-Method, Access-Control-Request-Headers",
            })(scope, receive, send)

        if origin != CHATGPT_ORIGIN:
            return await self.app(scope, receive, send)

        async def cors_send(message):
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers["Access-Control-Allow-Origin"] = CHATGPT_ORIGIN
                response_headers["Access-Control-Expose-Headers"] = "WWW-Authenticate, MCP-Session-Id, MCP-Protocol-Version"
                vary = response_headers.get("Vary", "")
                if "origin" not in {part.strip().lower() for part in vary.split(",")}:
                    response_headers["Vary"] = vary + ", Origin" if vary else "Origin"
            await send(message)

        return await self.app(scope, receive, cors_send)
