"""Общие зависимости эндпоинтов: сессия БД, текущий пользователь, CSRF."""

from __future__ import annotations

import ipaddress
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.errors import AppError, AuthenticationRequired, PermissionDenied
from app.logging import bind_context, get_logger
from app.models.users import User, UserSession
from app.security.passwords import PasswordService
from app.security.policies import is_admin
from app.security.rate_limit import LoginRateLimiter
from app.security.sessions import CSRF_HEADER, SESSION_COOKIE, SessionManager
from app.services.auth import AuthService
from app.services.users import UserService

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

log = get_logger(__name__)


class CsrfFailed(AppError):
    status_code = 403
    code = "csrf_failed"
    message = "Запрос отклонён защитой от подделки запросов. Обновите страницу и повторите."


class PasswordChangeRequired(AppError):
    status_code = 403
    code = "password_change_required"
    message = "Необходимо сменить пароль"


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_sessions(request: Request) -> SessionManager:
    manager: SessionManager = request.app.state.sessions
    return manager


def get_passwords(request: Request) -> PasswordService:
    service: PasswordService = request.app.state.passwords
    return service


def get_login_limiter(request: Request) -> LoginRateLimiter:
    limiter: LoginRateLimiter = request.app.state.login_limiter
    return limiter


def get_auth_service(request: Request) -> AuthService:
    service: AuthService = request.app.state.auth_service
    return service


def get_user_service(request: Request) -> UserService:
    service: UserService = request.app.state.user_service
    return service


Db = Annotated[AsyncSession, Depends(get_db)]
AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
UserServiceDep = Annotated[UserService, Depends(get_user_service)]
AppSettings = Annotated[Settings, Depends(get_settings_dep)]
Sessions = Annotated[SessionManager, Depends(get_sessions)]
Passwords = Annotated[PasswordService, Depends(get_passwords)]


def client_ip(request: Request) -> str | None:
    """IP клиента (uvicorn уже учёл X-Forwarded-For от доверенного прокси)."""
    host = request.client.host if request.client else None
    if not host:
        return None
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


def request_origin(request: Request) -> str | None:
    """Origin запроса; при его отсутствии — origin из Referer."""
    origin = request.headers.get("origin")
    if origin:
        return origin
    referer = request.headers.get("referer")
    if referer:
        parts = urlsplit(referer)
        return f"{parts.scheme}://{parts.netloc}"
    return None


def origin_allowed(request: Request, settings: Settings) -> bool:
    """Origin (или, если его нет, Referer) должен входить в ALLOWED_ORIGINS."""
    origin = request_origin(request)
    return origin is not None and origin in settings.allowed_origins


def _reject_csrf(request: Request, reason: str) -> CsrfFailed:
    # Origin не секрет; он нужен администратору, чтобы понять, какой адрес добавить в ALLOWED_ORIGINS.
    log.warning("csrf_rejected", reason=reason, origin=(request_origin(request) or "")[:200])
    return CsrfFailed()


def ensure_origin(request: Request, settings: Settings) -> None:
    if request.method not in SAFE_METHODS and not origin_allowed(request, settings):
        raise _reject_csrf(request, "origin_not_allowed")


@dataclass
class AuthContext:
    user: User
    session: UserSession
    ip: str | None


async def _authenticate(request: Request, db: AsyncSession, *, allow_password_change: bool) -> AuthContext:
    settings: Settings = request.app.state.settings
    manager: SessionManager = request.app.state.sessions
    resolved = await manager.resolve(db, request.cookies.get(SESSION_COOKIE))
    if resolved is None:
        raise AuthenticationRequired()
    record, user = resolved
    if request.method not in SAFE_METHODS:
        if not origin_allowed(request, settings):
            raise _reject_csrf(request, "origin_not_allowed")
        if not manager.verify_csrf(record, request.headers.get(CSRF_HEADER)):
            raise _reject_csrf(request, "token_mismatch")
    if user.must_change_password and not allow_password_change:
        raise PasswordChangeRequired()
    if await manager.touch(db, record):
        await db.commit()
    bind_context(user_id=str(user.id))
    return AuthContext(user=user, session=record, ip=client_ip(request))


async def require_auth(request: Request, db: Db) -> AuthContext:
    return await _authenticate(request, db, allow_password_change=False)


async def require_auth_allow_password_change(request: Request, db: Db) -> AuthContext:
    """Для /auth/me, /auth/change-password, /auth/logout — доступны до смены временного пароля."""
    return await _authenticate(request, db, allow_password_change=True)


Auth = Annotated[AuthContext, Depends(require_auth)]
AuthPasswordChange = Annotated[AuthContext, Depends(require_auth_allow_password_change)]


async def require_admin(auth: Auth) -> AuthContext:
    if not is_admin(auth.user):
        raise PermissionDenied()
    return auth


AdminAuth = Annotated[AuthContext, Depends(require_admin)]
