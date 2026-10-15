"""Вход, смена пароля. Сообщения об ошибках не раскрывают, существует ли e-mail."""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.logging import get_logger
from app.models.users import User
from app.security.passwords import PasswordPolicyError, PasswordService, validate_password
from app.security.rate_limit import LoginRateLimiter
from app.security.sessions import IssuedSession, RevokeReason, SessionManager, utcnow
from app.services.audit import AuditAction, record

log = get_logger(__name__)


class InvalidCredentials(AppError):
    status_code = 401
    code = "invalid_credentials"
    message = "Неверный e-mail или пароль"


class AccountDisabled(AppError):
    status_code = 403
    code = "account_disabled"
    message = "Учётная запись заблокирована. Обратитесь к администратору."


class LoginRateLimited(AppError):
    status_code = 429
    code = "login_rate_limited"
    message = "Слишком много неудачных попыток входа. Повторите позже."


class PasswordPolicyViolation(AppError):
    status_code = 422
    code = "password_policy"


def normalize_login_email(value: str) -> str:
    return value.strip().lower()


async def find_user_by_email(db: AsyncSession, email: str) -> User | None:
    stmt = select(User).where(func.lower(User.email) == normalize_login_email(email))
    return (await db.execute(stmt)).scalar_one_or_none()


def check_password_policy(password: str, *, email: str | None, display_name: str | None) -> None:
    try:
        validate_password(password, email=email, display_name=display_name)
    except PasswordPolicyError as exc:
        raise PasswordPolicyViolation(exc.message, details={"rule": exc.code}) from None


class AuthService:
    def __init__(
        self, passwords: PasswordService, sessions: SessionManager, limiter: LoginRateLimiter
    ) -> None:
        self.passwords = passwords
        self.sessions = sessions
        self.limiter = limiter

    async def login(
        self, db: AsyncSession, *, email: str, password: str, ip: str | None, user_agent: str | None
    ) -> tuple[User, IssuedSession]:
        email = normalize_login_email(email)
        email_hash = self.limiter.email_hash(email)

        state = await self.limiter.check(db, email_hash, ip)
        if state.retry_after_s is not None:
            record(
                db,
                AuditAction.login_failed,
                actor_id=None,
                ip=ip,
                details={"reason": "rate_limited", "email_hash": email_hash[:16]},
            )
            await db.commit()
            raise LoginRateLimited(headers={"Retry-After": str(state.retry_after_s)})

        user = await find_user_by_email(db, email)
        if user is None:
            self.passwords.verify_dummy(password)
            await self._fail(db, email_hash, ip, state.email_failures + 1, reason="unknown_user")
            raise InvalidCredentials()

        if not self.passwords.verify(user.password_hash, password):
            failures = state.email_failures + 1
            user.failed_login_count += 1
            if self.limiter.reached_limit(failures):
                user.locked_until = utcnow() + self.limiter.window
            await self._fail(db, email_hash, ip, failures, reason="invalid_password", user=user)
            raise InvalidCredentials()

        if not user.is_active:
            await self._fail(
                db, email_hash, ip, state.email_failures + 1, reason="account_disabled", user=user
            )
            raise AccountDisabled()

        if self.passwords.needs_rehash(user.password_hash):
            user.password_hash = self.passwords.hash(password)
        now = utcnow()
        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = now
        await self.limiter.register(db, email_hash, ip, success=True)
        issued = await self.sessions.create(db, user, ip=ip, user_agent=user_agent)
        record(
            db,
            AuditAction.login_success,
            actor_id=user.id,
            target_type="user",
            target_id=user.id,
            ip=ip,
            details={"session": str(issued.record.id)},
        )
        await db.commit()
        log.info("login_success", user_id=str(user.id))
        return user, issued

    async def _fail(
        self,
        db: AsyncSession,
        email_hash: str,
        ip: str | None,
        failures: int,
        *,
        reason: str,
        user: User | None = None,
    ) -> None:
        await self.limiter.register(db, email_hash, ip, success=False)
        # Для несуществующего пользователя пишем только префикс HMAC адреса — не сам введённый текст
        # (в поле e-mail по ошибке нередко вводят пароль).
        details: dict[str, object] = {"reason": reason, "email_hash": email_hash[:16]}
        record(
            db,
            AuditAction.login_failed,
            actor_id=None,
            target_type="user" if user else None,
            target_id=user.id if user else None,
            ip=ip,
            details=details,
        )
        await db.commit()
        log.info("login_failed", reason=reason)
        delay = self.limiter.failure_delay_s(failures)
        if delay > 0:
            await asyncio.sleep(delay)

    async def change_password(
        self,
        db: AsyncSession,
        user: User,
        *,
        current_password: str,
        new_password: str,
        ip: str | None,
        user_agent: str | None,
    ) -> IssuedSession:
        if not self.passwords.verify(user.password_hash, current_password):
            raise AppError("Текущий пароль указан неверно", code="invalid_current_password", status_code=400)
        if current_password == new_password:
            raise PasswordPolicyViolation(
                "Новый пароль должен отличаться от текущего", details={"rule": "password_reused"}
            )
        check_password_policy(new_password, email=user.email, display_name=user.display_name)
        user.password_hash = self.passwords.hash(new_password)
        user.password_changed_at = utcnow()
        user.must_change_password = False
        revoked = await self.sessions.revoke_all(db, user.id, RevokeReason.password_changed)
        issued = await self.sessions.create(db, user, ip=ip, user_agent=user_agent)
        record(
            db,
            AuditAction.password_changed,
            actor_id=user.id,
            target_type="user",
            target_id=user.id,
            ip=ip,
            details={"revoked_sessions": revoked},
        )
        await db.commit()
        return issued
