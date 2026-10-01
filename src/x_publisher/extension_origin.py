"""Public extension identity; this is an origin allowlist, never authentication."""
from .core import PREFIX

EXTENSION_ORIGIN = "chrome-extension://aogeffggcocbkdmfpiofickkfbdjifbb"

class ExtensionCORS:
    """Allow only the installed extension origin on the three pairing JSON routes."""
    paths = frozenset({PREFIX+"/oauth/pair/start", PREFIX+"/oauth/pair/poll", PREFIX+"/session-import"})

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] not in self.paths:
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        allowed = headers.get(b"origin") == EXTENSION_ORIGIN.encode()
        if scope["method"] == "OPTIONS":
            from starlette.responses import Response
            requested = {h.strip().lower() for h in headers.get(b"access-control-request-headers", b"").split(b",") if h.strip()}
            if not allowed or headers.get(b"access-control-request-method") != b"POST" or requested - {b"content-type", b"authorization"}:
                return await Response(status_code=403)(scope, receive, send)
            return await Response(status_code=204, headers={"Access-Control-Allow-Origin": EXTENSION_ORIGIN,
                "Access-Control-Allow-Methods": "POST", "Access-Control-Allow-Headers": "Content-Type, Authorization",
                "Access-Control-Max-Age": "300", "Vary": "Origin", "Cache-Control": "no-store"})(scope, receive, send)
        async def cors_send(message):
            if allowed and message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []),
                    (b"access-control-allow-origin", EXTENSION_ORIGIN.encode()), (b"vary", b"Origin")]
            await send(message)
        return await self.app(scope, receive, cors_send)
