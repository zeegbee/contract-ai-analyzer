"""Управление пользователями: создание, роли, блокировка, сброс пароля, сессии, доступ к моделям.

Права проверяются политиками (`app.security.policies`) до любых изменений. Инвариант
«хотя бы один активный Superadmin» проверяется под блокировкой строк в той же транзакции.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, delete, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError, AuthenticationRequired, ConflictError, NotFoundError, PermissionDenied
from app.models.catalog import ModelConfig
from app.models.enums import UserRole
from app.models.users import User, UserModelPermission, UserSession
from app.schemas.users import ModelPermissionItem, ModelPermissionOverride
from app.security import policies
from app.security.passwords import PasswordService, generate_temporary_password
from app.security.rate_limit import LoginRateLimiter
from app.security.sessions import RevokeReason, SessionManager, utcnow
from app.services.audit import AuditAction, record

MAX_PAGE_SIZE = 100
# Ключ pg_advisory_xact_lock: административные изменения пользователей выполняются последовательно.
_USER_ADMIN_LOCK_KEY = 0x6361_6975_7365_7202


class LastSuperadmin(ConflictError):
    code = "last_superadmin"
    message = "Нельзя понизить или заблокировать последнего активного Superadmin"


class EmailTaken(ConflictError):
    code = "email_taken"
    message = "Пользователь с таким e-mail уже существует"


class InvalidCursor(AppError):
    code = "invalid_cursor"
    message = "Некорректный параметр пагинации"


@dataclass
class Actor:
    """Кто выполняет действие (для аудита)."""

    user: User
    ip: str | None


async def begin_admin_change(db: AsyncSession, actor: Actor) -> None:
    """Сериализует административные изменения и перечитывает актора внутри транзакции.

    Права проверяются по актуальному состоянию: если актора успели понизить или заблокировать
    параллельным запросом, его изменение будет отклонено. Единая блокировка исключает deadlock
    при встречных изменениях (два Superadmin понижают друг друга) и гонки в инварианте
    «хотя бы один активный Superadmin».
    """
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _USER_ADMIN_LOCK_KEY})
    stmt = select(User).where(User.id == actor.user.id).execution_options(populate_existing=True)
    fresh = (await db.execute(stmt)).scalar_one_or_none()
    if fresh is None or not fresh.is_active:
        raise AuthenticationRequired()
    actor.user = fresh


async def get_user(db: AsyncSession, user_id: uuid.UUID, *, for_update: bool = False) -> User:
    stmt = select(User).where(User.id == user_id)
    if for_update:
        stmt = stmt.with_for_update()
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None:
        raise NotFoundError("Пользователь не найден", code="user_not_found")
    return user


async def count_active_sessions(db: AsyncSession, user_id: uuid.UUID) -> int:
    now = utcnow()
    stmt = select(func.count()).where(
        UserSession.user_id == user_id,
        UserSession.revoked_at.is_(None),
        UserSession.expires_at > now,
        UserSession.idle_expires_at > now,
    )
    return int((await db.execute(stmt)).scalar_one())


# ------------------------------------------------------------------ пагинация


def encode_cursor(created_at: datetime, user_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}|{user_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        created, uid = base64.urlsafe_b64decode(padded.encode()).decode().split("|", 1)
        return datetime.fromisoformat(created), uuid.UUID(uid)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise InvalidCursor() from None


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def list_users(
    db: AsyncSession,
    *,
    limit: int,
    cursor: str | None,
    query: str | None,
    role: UserRole | None,
    is_active: bool | None,
) -> tuple[list[User], str | None]:
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    stmt = select(User)
    if query:
        pattern = f"%{_escape_like(query.strip().lower())}%"
        stmt = stmt.where(
            or_(
                func.lower(User.email).like(pattern, escape="\\"),
                func.lower(User.display_name).like(pattern, escape="\\"),
            )
        )
    if role is not None:
        stmt = stmt.where(User.role == role)
    if is_active is not None:
        stmt = stmt.where(User.is_active.is_(is_active))
    if cursor:
        created, uid = decode_cursor(cursor)
        stmt = stmt.where(or_(User.created_at > created, and_(User.created_at == created, User.id > uid)))
    stmt = stmt.order_by(User.created_at, User.id).limit(limit + 1)
    rows = list((await db.execute(stmt)).scalars())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = encode_cursor(last.created_at, last.id)
    return rows, next_cursor


# ------------------------------------------------------------------ изменения


class UserService:
    def __init__(
        self, passwords: PasswordService, sessions: SessionManager, limiter: LoginRateLimiter
    ) -> None:
        self.passwords = passwords
        self.sessions = sessions
        self.limiter = limiter

    async def create_user(
        self, db: AsyncSession, actor: Actor, *, email: str, display_name: str, role: UserRole
    ) -> tuple[User, str]:
        await begin_admin_change(db, actor)
        policies.ensure_can_manage_users(actor.user)
        if role not in policies.assignable_roles(actor.user):
            raise PermissionDenied("Недостаточно прав для назначения этой роли", code="role_not_assignable")
        temporary = generate_temporary_password()
        user = User(
            id=uuid.uuid4(),
            email=email,
            display_name=display_name,
            password_hash=self.passwords.hash(temporary),
            role=role,
            is_active=True,
            must_change_password=True,
            created_by_id=actor.user.id,
        )
        db.add(user)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            raise EmailTaken() from None
        record(
            db,
            AuditAction.user_created,
            actor_id=actor.user.id,
            target_type="user",
            target_id=user.id,
            ip=actor.ip,
            details={"role": role.value},
        )
        await db.commit()
        await db.refresh(user)
        return user, temporary

    async def _ensure_not_last_superadmin(self, db: AsyncSession, target: User) -> None:
        if target.role is not UserRole.superadmin or not target.is_active:
            return
        stmt = (
            select(User.id)
            .where(User.role == UserRole.superadmin, User.is_active.is_(True))
            .order_by(User.id)
            .with_for_update()
        )
        active_ids = list((await db.execute(stmt)).scalars())
        if len(active_ids) <= 1:
            raise LastSuperadmin()

    async def update_user(
        self,
        db: AsyncSession,
        actor: Actor,
        user_id: uuid.UUID,
        *,
        display_name: str | None,
        role: UserRole | None,
        is_active: bool | None,
    ) -> User:
        await begin_admin_change(db, actor)
        target = await get_user(db, user_id, for_update=True)
        policies.ensure_can_modify_user(actor.user, target)

        role_changed = role is not None and role is not target.role
        active_changed = is_active is not None and is_active != target.is_active
        if role_changed:
            assert role is not None
            policies.ensure_can_assign_role(actor.user, target, role)
        if active_changed:
            policies.ensure_can_set_active(actor.user, target)
        if (role_changed and target.role is UserRole.superadmin) or (active_changed and not is_active):
            await self._ensure_not_last_superadmin(db, target)

        if display_name is not None and display_name != target.display_name:
            target.display_name = display_name
            record(
                db,
                AuditAction.user_updated,
                actor_id=actor.user.id,
                target_type="user",
                target_id=target.id,
                ip=actor.ip,
                details={"fields": ["display_name"]},
            )
        if role_changed:
            assert role is not None
            record(
                db,
                AuditAction.user_role_changed,
                actor_id=actor.user.id,
                target_type="user",
                target_id=target.id,
                ip=actor.ip,
                details={"from": target.role.value, "to": role.value},
            )
            target.role = role
        if active_changed:
            assert is_active is not None
            target.is_active = is_active
            revoked = 0
            if not is_active:
                revoked = await self.sessions.revoke_all(db, target.id, RevokeReason.user_blocked)
            record(
                db,
                AuditAction.user_unblocked if is_active else AuditAction.user_blocked,
                actor_id=actor.user.id,
                target_type="user",
                target_id=target.id,
                ip=actor.ip,
                details={"revoked_sessions": revoked} if not is_active else {},
            )
        await db.commit()
        await db.refresh(target)
        return target

    async def reset_password(self, db: AsyncSession, actor: Actor, user_id: uuid.UUID) -> tuple[User, str]:
        await begin_admin_change(db, actor)
        target = await get_user(db, user_id, for_update=True)
        policies.ensure_can_modify_user(actor.user, target)
        if target.id == actor.user.id:
            raise PermissionDenied(
                "Для своей учётной записи используйте смену пароля", code="cannot_reset_own_password"
            )
        temporary = generate_temporary_password()
        target.password_hash = self.passwords.hash(temporary)
        target.must_change_password = True
        target.password_changed_at = utcnow()
        target.failed_login_count = 0
        target.locked_until = None
        await self.limiter.reset(db, self.limiter.email_hash(target.email))
        revoked = await self.sessions.revoke_all(db, target.id, RevokeReason.password_reset)
        record(
            db,
            AuditAction.user_password_reset,
            actor_id=actor.user.id,
            target_type="user",
            target_id=target.id,
            ip=actor.ip,
            details={"revoked_sessions": revoked},
        )
        await db.commit()
        await db.refresh(target)
        return target, temporary

    async def revoke_sessions(self, db: AsyncSession, actor: Actor, user_id: uuid.UUID) -> int:
        await begin_admin_change(db, actor)
        target = await get_user(db, user_id)
        policies.ensure_can_modify_user(actor.user, target)
        revoked = await self.sessions.revoke_all(db, target.id, RevokeReason.revoked_by_admin)
        record(
            db,
            AuditAction.user_sessions_revoked,
            actor_id=actor.user.id,
            target_type="user",
            target_id=target.id,
            ip=actor.ip,
            details={"revoked_sessions": revoked},
        )
        await db.commit()
        return revoked

    async def unlock_login(self, db: AsyncSession, actor: Actor, user_id: uuid.UUID) -> User:
        await begin_admin_change(db, actor)
        target = await get_user(db, user_id, for_update=True)
        policies.ensure_can_modify_user(actor.user, target)
        target.failed_login_count = 0
        target.locked_until = None
        await self.limiter.reset(db, self.limiter.email_hash(target.email))
        record(
            db,
            AuditAction.user_login_unlocked,
            actor_id=actor.user.id,
            target_type="user",
            target_id=target.id,
            ip=actor.ip,
        )
        await db.commit()
        await db.refresh(target)
        return target


# ------------------------------------------------------------------ доступ к моделям


def model_effective_access(user: User, model: ModelConfig, override: bool | None) -> bool:
    """Доступна ли модель пользователю: модель включена и (персональное правило, иначе — роль).

    Состояние провайдера и глобальный выключатель облака учитываются в каталоге моделей (этап 05).
    """
    if not model.enabled:
        return False
    if override is not None:
        return override
    return user.role.value in model.allowed_roles


async def model_permissions(db: AsyncSession, user: User) -> list[ModelPermissionItem]:
    overrides = {
        row.model_config_id: row.allowed
        for row in (
            await db.execute(select(UserModelPermission).where(UserModelPermission.user_id == user.id))
        ).scalars()
    }
    models = (await db.execute(select(ModelConfig).order_by(ModelConfig.display_name))).scalars()
    items = []
    for model in models:
        override = overrides.get(model.id)
        items.append(
            ModelPermissionItem(
                model_config_id=model.id,
                display_name=model.display_name,
                external_model_id=model.external_model_id,
                enabled=model.enabled,
                allowed_by_role=user.role.value in model.allowed_roles,
                override=override,
                effective=model_effective_access(user, model, override),
            )
        )
    return items


async def set_model_permissions(
    db: AsyncSession, actor: Actor, user_id: uuid.UUID, overrides: list[ModelPermissionOverride]
) -> User:
    await begin_admin_change(db, actor)
    target = await get_user(db, user_id, for_update=True)
    policies.ensure_can_modify_user(actor.user, target)
    wanted = {o.model_config_id: o.allowed for o in overrides}
    if wanted:
        existing = set(
            (await db.execute(select(ModelConfig.id).where(ModelConfig.id.in_(wanted.keys())))).scalars()
        )
        unknown = set(wanted) - existing
        if unknown:
            raise AppError("Указана несуществующая модель", code="model_not_found", status_code=422)
    before = {
        row.model_config_id: row.allowed
        for row in (
            await db.execute(select(UserModelPermission).where(UserModelPermission.user_id == target.id))
        ).scalars()
    }
    await db.execute(delete(UserModelPermission).where(UserModelPermission.user_id == target.id))
    for model_id, allowed in wanted.items():
        db.add(UserModelPermission(user_id=target.id, model_config_id=model_id, allowed=allowed))
    changes = {
        str(mid): {"from": before.get(mid), "to": wanted.get(mid)}
        for mid in set(before) | set(wanted)
        if before.get(mid) != wanted.get(mid)
    }
    record(
        db,
        AuditAction.user_model_permissions_changed,
        actor_id=actor.user.id,
        target_type="user",
        target_id=target.id,
        ip=actor.ip,
        details={"changes": changes},
    )
    await db.commit()
    return target
