"""可选的 Bearer Token 鉴权（设置 API_TOKEN 后启用）。

纯 ASGI 中间件，同时覆盖 HTTP 与 WebSocket（含 a2a-sdk 挂载的 A2A 路由）。
WebSocket 客户端可用 `Authorization: Bearer <token>` 头或 `?token=<token>` 查询参数。
"""

import hmac
from urllib.parse import parse_qs

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

PUBLIC_PATHS = ("/health", "/.well-known/", "/docs", "/redoc", "/openapi.json")


class BearerTokenMiddleware:
    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._token = token.encode()

    def _authorized(self, scope: Scope) -> bool:
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode()
        provided = auth[7:] if auth.lower().startswith("bearer ") else ""
        if not provided and scope["type"] == "websocket":
            provided = parse_qs(scope.get("query_string", b"").decode()).get("token", [""])[0]
        return hmac.compare_digest(provided.encode(), self._token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        public = path == "/" or path.startswith(PUBLIC_PATHS)
        if scope["type"] not in ("http", "websocket") or public or scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return
        if self._authorized(scope):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401, "reason": "unauthorized"})
            return
        response = JSONResponse(
            {"detail": "未授权：需要 Authorization: Bearer <API_TOKEN>"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)
