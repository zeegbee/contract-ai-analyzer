"""Единый формат ошибок API.

Каждая ошибка возвращается как:
    {"error": {"code": "...", "message": "...", "request_id": "...", "details": {...}}}

`message` безопасен для показа пользователю. Внутренние детали (стектрейсы, тексты исключений,
значения полей запроса) в ответ не попадают.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging import get_logger
from app.request_context import get_request_id

log = get_logger(__name__)


class AppError(Exception):
    """Базовая ошибка приложения с безопасным сообщением."""

    status_code = 400
    code = "bad_request"
    message = "Некорректный запрос"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.details = details or {}
        self.headers = headers
        super().__init__(self.code)


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"
    message = "Объект не найден"


class AuthenticationRequired(AppError):
    status_code = 401
    code = "authentication_required"
    message = "Требуется вход в систему"


class PermissionDenied(AppError):
    status_code = 403
    code = "permission_denied"
    message = "Недостаточно прав"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"
    message = "Конфликт состояния"


class ServiceUnavailable(AppError):
    status_code = 503
    code = "service_unavailable"
    message = "Сервис временно недоступен"


_HTTP_STATUS_CODES: dict[int, tuple[str, str]] = {
    400: ("bad_request", "Некорректный запрос"),
    401: ("authentication_required", "Требуется вход в систему"),
    403: ("permission_denied", "Недостаточно прав"),
    404: ("not_found", "Ресурс не найден"),
    405: ("method_not_allowed", "Метод не поддерживается"),
    406: ("not_acceptable", "Формат ответа не поддерживается"),
    409: ("conflict", "Конфликт состояния"),
    413: ("payload_too_large", "Слишком большой запрос"),
    415: ("unsupported_media_type", "Неподдерживаемый тип данных"),
    429: ("rate_limited", "Слишком много запросов, попробуйте позже"),
}


def error_body(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": get_request_id(),
            "details": details or {},
        }
    }


def _json(status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content=body, headers=headers)


async def _app_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    if exc.status_code >= 500:
        log.warning("app_error", code=exc.code, status=exc.status_code)
    return _json(exc.status_code, error_body(exc.code, exc.message, exc.details), exc.headers)


async def _http_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code, message = _HTTP_STATUS_CODES.get(exc.status_code, ("http_error", "Ошибка запроса"))
    if exc.status_code >= 500:
        code, message = "internal_error", "Внутренняя ошибка сервера"
    headers = dict(exc.headers) if exc.headers else None
    return _json(exc.status_code, error_body(code, message), headers)


async def _validation_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    # Значения полей (`input`) не возвращаем и не логируем: там может быть текст договора или пароль.
    fields = [
        {"loc": [str(p) for p in err.get("loc", ())], "type": err.get("type", "invalid")}
        for err in exc.errors()
    ]
    return _json(
        422,
        error_body("validation_error", "Проверьте корректность введённых данных", {"fields": fields}),
    )


async def _unhandled_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    log.error("unhandled_exception", exc_type=type(exc).__name__, exc_info=exc)
    return _json(500, error_body("internal_error", "Внутренняя ошибка сервера"))


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)
