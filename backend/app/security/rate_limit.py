"""Ограничение попыток входа (скользящее окно в PostgreSQL).

- По e-mail: не более N неудач в окне после последнего успешного входа. Работает одинаково для
  существующих и несуществующих адресов — по ответу нельзя узнать, есть ли такой пользователь.
- По IP: не более M неудач в окне.
- E-mail хранится только как HMAC (`email_hash`), сам адрес в таблицу попыток не пишется.
- Попытки, отклонённые лимитом, не считаются новыми неудачами (иначе блокировка продлевалась бы бесконечно).
- Сброс блокировки администратором — запись-маркер `success=true` без IP.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.users import LoginAttempt
from app.security.sessions import utcnow
from app.security.tokens import keyed_hash

# Записи старше этого срока не участвуют в лимитах и удаляются при очередных попытках входа.
ATTEMPTS_RETENTION = timedelta(days=1)


@dataclass(frozen=True)
class LimitState:
    email_failures: int
    retry_after_s: int | None  # None — вход разрешён


class LoginRateLimiter:
    def __init__(self, settings: Settings) -> None:
        self._secret = settings.secret_key.get_secret_value()
        self._window = timedelta(minutes=settings.login_window_minutes)
        self._max_email = settings.login_max_failures_per_email
        self._max_ip = settings.login_max_failures_per_ip
        self._step_ms = settings.login_delay_step_ms
        self._max_delay_ms = settings.login_delay_max_ms

    @property
    def window(self) -> timedelta:
        return self._window

    def email_hash(self, normalized_email: str) -> str:
        return keyed_hash(self._secret, "login-email", normalized_email)

    async def _email_failures(
        self, db: AsyncSession, email_hash: str, now: datetime
    ) -> tuple[int, datetime | None]:
        last_success = (
            select(func.max(LoginAttempt.created_at))
            .where(LoginAttempt.email_hash == email_hash, LoginAttempt.success.is_(True))
            .scalar_subquery()
        )
        stmt = select(func.count(), func.min(LoginAttempt.created_at)).where(
            LoginAttempt.email_hash == email_hash,
            LoginAttempt.success.is_(False),
            LoginAttempt.created_at > now - self._window,
            func.coalesce(LoginAttempt.created_at > last_success, true()),
        )
        count, oldest = (await db.execute(stmt)).one()
        return int(count), oldest

    async def _ip_failures(self, db: AsyncSession, ip: str, now: datetime) -> tuple[int, datetime | None]:
        stmt = select(func.count(), func.min(LoginAttempt.created_at)).where(
            LoginAttempt.ip == ip,
            LoginAttempt.success.is_(False),
            LoginAttempt.created_at > now - self._window,
        )
        count, oldest = (await db.execute(stmt)).one()
        return int(count), oldest

    def _retry_after(self, oldest: datetime | None, now: datetime) -> int:
        if oldest is None:
            return 1
        return max(1, math.ceil((oldest + self._window - now).total_seconds()))

    async def check(self, db: AsyncSession, email_hash: str, ip: str | None) -> LimitState:
        now = utcnow()
        email_failures, email_oldest = await self._email_failures(db, email_hash, now)
        if email_failures >= self._max_email:
            return LimitState(email_failures, self._retry_after(email_oldest, now))
        if ip:
            ip_failures, ip_oldest = await self._ip_failures(db, ip, now)
            if ip_failures >= self._max_ip:
                return LimitState(email_failures, self._retry_after(ip_oldest, now))
        return LimitState(email_failures, None)

    async def register(self, db: AsyncSession, email_hash: str, ip: str | None, *, success: bool) -> None:
        db.add(LoginAttempt(email_hash=email_hash, ip=ip, success=success))
        await db.execute(delete(LoginAttempt).where(LoginAttempt.created_at < utcnow() - ATTEMPTS_RETENTION))

    async def reset(self, db: AsyncSession, email_hash: str) -> None:
        """Сброс блокировки по e-mail (действие администратора)."""
        db.add(LoginAttempt(email_hash=email_hash, ip=None, success=True))

    def failure_delay_s(self, failures_including_current: int) -> float:
        """Прогрессивная задержка ответа: первые две неудачи — без задержки."""
        steps = max(0, failures_including_current - 2)
        return min(self._step_ms * steps, self._max_delay_ms) / 1000

    def reached_limit(self, failures_including_current: int) -> bool:
        return failures_including_current >= self._max_email
