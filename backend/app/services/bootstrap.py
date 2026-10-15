"""Создание первого Superadmin из переменных окружения.

Гарантии:
- одна транзакция под advisory lock — параллельный запуск (несколько реплик) создаёт не более одного
  пользователя;
- идемпотентность: если Superadmin уже есть или bootstrap уже выполнялся, ничего не меняется —
  ни пароль, ни роль существующих пользователей;
- пароля по умолчанию нет: без BOOTSTRAP_ADMIN_* пользователь не создаётся, вход невозможен;
- пароль проходит политику сложности и никогда не логируется;
- созданный пользователь обязан сменить пароль при первом входе.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import exists, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.logging import get_logger
from app.models.enums import UserRole
from app.models.system import SystemSetting
from app.models.users import User
from app.schemas.users import normalize_email
from app.security.passwords import PasswordPolicyError, PasswordService, validate_password
from app.services.audit import AuditAction, record
from app.services.auth import find_user_by_email

log = get_logger(__name__)

# Ключ pg_advisory_xact_lock (произвольная константа, уникальная для bootstrap).
_BOOTSTRAP_LOCK_KEY = 0x6361_6962_6F6F_7401
BOOTSTRAP_SETTING = "bootstrap_completed"


class BootstrapOutcome(StrEnum):
    created = "created"
    skipped_superadmin_exists = "skipped_superadmin_exists"
    skipped_already_completed = "skipped_already_completed"
    disabled = "disabled"
    missing_parameters = "missing_parameters"
    invalid_parameters = "invalid_parameters"
    email_conflict = "email_conflict"


# Исходы, при которых система остаётся без администратора — это ошибка конфигурации.
FAILED_OUTCOMES = frozenset(
    {
        BootstrapOutcome.missing_parameters,
        BootstrapOutcome.invalid_parameters,
        BootstrapOutcome.email_conflict,
    }
)


@dataclass(frozen=True)
class BootstrapResult:
    outcome: BootstrapOutcome
    # Безопасное описание (без значений переменных).
    message: str
    user_id: uuid.UUID | None = None


async def superadmin_exists(db: AsyncSession, *, active_only: bool = False) -> bool:
    cond = [User.role == UserRole.superadmin]
    if active_only:
        cond.append(User.is_active.is_(True))
    return bool((await db.execute(select(exists().where(*cond)))).scalar_one())


async def _bootstrap_completed(db: AsyncSession) -> bool:
    value = (
        await db.execute(select(SystemSetting.value).where(SystemSetting.key == BOOTSTRAP_SETTING))
    ).scalar_one_or_none()
    return value is True


def _validate_parameters(settings: Settings) -> tuple[str, str, str] | BootstrapResult:
    email_raw = settings.bootstrap_admin_email
    password_secret = settings.bootstrap_admin_password
    name = (settings.bootstrap_admin_name or "").strip()
    missing = [
        var
        for var, value in (
            ("BOOTSTRAP_ADMIN_EMAIL", email_raw),
            ("BOOTSTRAP_ADMIN_PASSWORD", password_secret),
            ("BOOTSTRAP_ADMIN_NAME", name),
        )
        if not value
    ]
    if missing:
        return BootstrapResult(
            BootstrapOutcome.missing_parameters,
            "Superadmin отсутствует, а параметры не заданы: " + ", ".join(missing),
        )
    assert email_raw is not None and password_secret is not None
    try:
        email = normalize_email(email_raw)
    except ValueError:
        return BootstrapResult(
            BootstrapOutcome.invalid_parameters, "BOOTSTRAP_ADMIN_EMAIL: некорректный формат"
        )
    if len(name) > 200:
        return BootstrapResult(
            BootstrapOutcome.invalid_parameters, "BOOTSTRAP_ADMIN_NAME: длиннее 200 символов"
        )
    password = password_secret.get_secret_value()
    try:
        validate_password(password, email=email, display_name=name)
    except PasswordPolicyError as exc:
        return BootstrapResult(
            BootstrapOutcome.invalid_parameters,
            f"BOOTSTRAP_ADMIN_PASSWORD не соответствует политике: {exc.message}",
        )
    return email, password, name


async def run_bootstrap(db: AsyncSession, settings: Settings, passwords: PasswordService) -> BootstrapResult:
    """Выполняет bootstrap в собственной транзакции. `db` не должен иметь начатых изменений."""
    if not settings.bootstrap_enabled:
        return BootstrapResult(BootstrapOutcome.disabled, "Bootstrap отключён (BOOTSTRAP_ENABLED=false)")

    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _BOOTSTRAP_LOCK_KEY})
    if await superadmin_exists(db):
        await db.rollback()
        return BootstrapResult(
            BootstrapOutcome.skipped_superadmin_exists, "Superadmin уже существует — bootstrap пропущен"
        )
    if await _bootstrap_completed(db):
        await db.rollback()
        return BootstrapResult(
            BootstrapOutcome.skipped_already_completed,
            "Bootstrap уже выполнялся ранее — повторно не выполняется",
        )

    params = _validate_parameters(settings)
    if isinstance(params, BootstrapResult):
        await db.rollback()
        return params
    email, password, name = params

    if await find_user_by_email(db, email) is not None:
        # Существующего пользователя не повышаем и не меняем ему пароль.
        await db.rollback()
        return BootstrapResult(
            BootstrapOutcome.email_conflict,
            "Пользователь с BOOTSTRAP_ADMIN_EMAIL уже существует; его роль и пароль не изменены",
        )

    user = User(
        id=uuid.uuid4(),
        email=email,
        display_name=name,
        password_hash=passwords.hash(password),
        role=UserRole.superadmin,
        is_active=True,
        must_change_password=True,
    )
    db.add(user)
    await db.flush()
    await db.execute(
        insert(SystemSetting)
        .values(key=BOOTSTRAP_SETTING, value=True, updated_by_id=user.id)
        .on_conflict_do_update(index_elements=[SystemSetting.key], set_={"value": True})
    )
    record(db, AuditAction.user_bootstrap_created, actor_id=None, target_type="user", target_id=user.id)
    await db.commit()
    return BootstrapResult(BootstrapOutcome.created, "Создан первый Superadmin", user_id=user.id)


def log_result(result: BootstrapResult, settings: Settings) -> None:
    fields = {"outcome": result.outcome.value}
    if result.user_id:
        fields["user_id"] = str(result.user_id)
    if result.outcome in FAILED_OUTCOMES:
        log.error("bootstrap_failed", detail=result.message, **fields)
    else:
        log.info("bootstrap", detail=result.message, **fields)
    skipped = {BootstrapOutcome.skipped_superadmin_exists, BootstrapOutcome.skipped_already_completed}
    if result.outcome in skipped and settings.bootstrap_admin_password is not None:
        log.warning(
            "bootstrap_password_still_configured",
            detail="Удалите BOOTSTRAP_ADMIN_PASSWORD из окружения и установите BOOTSTRAP_ENABLED=false",
        )
