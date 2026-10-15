"""Серверные сессии в PostgreSQL.

- Токен сессии (256 бит) живёт только в HttpOnly cookie `cai_session`; в БД — его HMAC.
- CSRF-токен (synchronizer token) — в cookie `cai_csrf`, доступной SPA; в БД — его HMAC.
  SPA возвращает его в заголовке `X-CSRF-Token`, сервер сравнивает в постоянном времени.
- TTL: скользящий idle и абсолютный. Отозванная/истёкшая сессия не принимается.
- Новая сессия создаётся при каждом входе и смене пароля (защита от фиксации сессии).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.users import User, UserSession
from app.security.tokens import constant_time_equals, keyed_hash, new_token

SESSION_COOKIE = "cai_session"
CSRF_COOKIE = "cai_csrf"
CSRF_HEADER = "X-CSRF-Token"
# Cookie сессии нужна только API; CSRF-cookie читает SPA на любой странице.
SESSION_COOKIE_PATH = "/api"
CSRF_COOKIE_PATH = "/"
# last_seen_at/idle_expires_at обновляются не чаще, чем раз в этот интервал.
TOUCH_INTERVAL = timedelta(seconds=60)
MAX_USER_AGENT_LEN = 256

_SESSION_PURPOSE = "session"
_CSRF_PURPOSE = "csrf"


class RevokeReason:
    logout = "logout"
    password_changed = "password_changed"  # noqa: S105 - причина отзыва, не пароль
    password_reset = "password_reset"  # noqa: S105
    user_blocked = "user_blocked"
    role_changed = "role_changed"
    revoked_by_user = "revoked_by_user"
    revoked_by_admin = "revoked_by_admin"


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class IssuedSession:
    record: UserSession
    token: str
    csrf_token: str


class SessionManager:
    def __init__(self, settings: Settings) -> None:
        self._secret = settings.secret_key.get_secret_value()
        self._idle = timedelta(minutes=settings.session_idle_minutes)
        self._absolute = timedelta(hours=settings.session_absolute_hours)
        self._secure = settings.cookie_secure

    def token_hash(self, token: str) -> str:
        return keyed_hash(self._secret, _SESSION_PURPOSE, token)

    def csrf_hash(self, token: str) -> str:
        return keyed_hash(self._secret, _CSRF_PURPOSE, token)

    async def create(
        self, db: AsyncSession, user: User, *, ip: str | None, user_agent: str | None
    ) -> IssuedSession:
        now = utcnow()
        token, csrf_token = new_token(), new_token()
        expires_at = now + self._absolute
        record = UserSession(
            id=uuid.uuid4(),
            user_id=user.id,
            token_hash=self.token_hash(token),
            csrf_token_hash=self.csrf_hash(csrf_token),
            created_at=now,
            last_seen_at=now,
            expires_at=expires_at,
            idle_expires_at=min(now + self._idle, expires_at),
            ip=ip,
            user_agent=(user_agent or "")[:MAX_USER_AGENT_LEN] or None,
        )
        db.add(record)
        await db.flush()
        return IssuedSession(record=record, token=token, csrf_token=csrf_token)

    async def resolve(self, db: AsyncSession, token: str | None) -> tuple[UserSession, User] | None:
        """Возвращает действующую сессию и активного пользователя, иначе None."""
        if not token or len(token) > 128:
            return None
        now = utcnow()
        stmt = (
            select(UserSession, User)
            .join(User, User.id == UserSession.user_id)
            .where(
                UserSession.token_hash == self.token_hash(token),
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > now,
                UserSession.idle_expires_at > now,
                User.is_active.is_(True),
            )
        )
        row = (await db.execute(stmt)).first()
        if row is None:
            return None
        return row[0], row[1]

    async def touch(self, db: AsyncSession, record: UserSession) -> bool:
        """Продлевает idle-TTL. Возвращает True, если запись изменена (нужен commit)."""
        now = utcnow()
        if now - record.last_seen_at < TOUCH_INTERVAL:
            return False
        record.last_seen_at = now
        record.idle_expires_at = min(now + self._idle, record.expires_at)
        return True

    def verify_csrf(self, record: UserSession, header_value: str | None) -> bool:
        if not header_value or len(header_value) > 128:
            return False
        return constant_time_equals(self.csrf_hash(header_value), record.csrf_token_hash)

    async def revoke(self, db: AsyncSession, session_id: uuid.UUID, reason: str) -> None:
        await db.execute(
            update(UserSession)
            .where(UserSession.id == session_id, UserSession.revoked_at.is_(None))
            .values(revoked_at=utcnow(), revoked_reason=reason)
        )

    async def revoke_all(
        self, db: AsyncSession, user_id: uuid.UUID, reason: str, *, except_id: uuid.UUID | None = None
    ) -> int:
        stmt = (
            update(UserSession)
            .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
            .values(revoked_at=utcnow(), revoked_reason=reason)
        )
        if except_id is not None:
            stmt = stmt.where(UserSession.id != except_id)
        result = await db.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)

    async def active_sessions(self, db: AsyncSession, user_id: uuid.UUID) -> list[UserSession]:
        now = utcnow()
        stmt = (
            select(UserSession)
            .where(
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > now,
                UserSession.idle_expires_at > now,
            )
            .order_by(UserSession.last_seen_at.desc())
        )
        return list((await db.execute(stmt)).scalars())

    # ------------------------------------------------------------------ cookies

    def set_cookies(self, response: Response, issued: IssuedSession) -> None:
        max_age = int(self._absolute.total_seconds())
        response.set_cookie(
            SESSION_COOKIE,
            issued.token,
            max_age=max_age,
            path=SESSION_COOKIE_PATH,
            secure=self._secure,
            httponly=True,
            samesite="lax",
        )
        response.set_cookie(
            CSRF_COOKIE,
            issued.csrf_token,
            max_age=max_age,
            path=CSRF_COOKIE_PATH,
            secure=self._secure,
            httponly=False,
            samesite="lax",
        )

    def clear_cookies(self, response: Response) -> None:
        response.delete_cookie(
            SESSION_COOKIE, path=SESSION_COOKIE_PATH, secure=self._secure, httponly=True, samesite="lax"
        )
        response.delete_cookie(CSRF_COOKIE, path=CSRF_COOKIE_PATH, secure=self._secure, samesite="lax")
