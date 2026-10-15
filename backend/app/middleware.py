"""ASGI middleware: request ID, access-лог без query-строк, заголовки безопасности."""

from __future__ import annotations

import time

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.logging import bind_context, clear_context, get_logger
from app.request_context import new_request_id, set_request_id

log = get_logger("app.access")

_SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    # Ответы API могут содержать конфиденциальные данные — не кешировать.
    "Cache-Control": "no-store",
}
_API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
_DOCS_PATHS = ("/api/docs", "/api/openapi.json")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, *, hsts: bool = False) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = None
        for name, value in scope.get("headers", []):
            if name == b"x-request-id":
                incoming = value.decode("latin-1")
                break
        request_id = new_request_id(incoming)
        set_request_id(request_id)
        clear_context()
        bind_context(request_id=request_id)

        path: str = scope.get("path", "")
        method: str = scope.get("method", "")
        started = time.perf_counter()
        status_holder = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                for key, value in _SECURITY_HEADERS.items():
                    headers.setdefault(key, value)
                if not path.startswith(_DOCS_PATHS):
                    headers.setdefault("Content-Security-Policy", _API_CSP)
                if self.hsts:
                    headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            # Только путь, без query-строки: в параметрах могут оказаться данные пользователя.
            if not path.startswith("/api/health"):
                log.info(
                    "http_request",
                    method=method,
                    path=path,
                    status=status_holder["status"],
                    duration_ms=duration_ms,
                )
            set_request_id(None)
            clear_context()
