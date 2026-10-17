"""Настройки и журнал резервного копирования (этап 08A).

Разделение ответственности (docs/OPERATIONS.md §16):
- бэкап выполняет скрипт на хосте `deploy/backup.sh` (pg_dump, архив, шифрование age, выгрузка, ротация);
- приложение хранит **несекретные** настройки, которые Superadmin меняет в UI (расписание, цель из
  настроенных на сервере, сроки хранения, режим ротации), считает, пора ли запускать бэкап
  (`python -m app.cli backup-plan`), и принимает отчёты о запусках (`backup-report`);
- пути, адрес и учётные данные хранилища, публичный ключ шифрования и webhook задаются только в `.env`
  на сервере: изменить их через веб-интерфейс нельзя (иначе взлом учётной записи позволил бы увести
  бэкапы или ключи хранилища).

Эффективное значение параметра = значение из UI (если задано), иначе из `.env`.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.backup import cron
from app.config import Settings
from app.errors import AppError, NotFoundError
from app.logging import get_logger
from app.models.enums import BackupRunStatus, BackupTrigger
from app.models.system import BackupRun, ConfigRevision, SystemSetting
from app.models.users import User
from app.security import policies
from app.services.audit import AuditAction, record

log = get_logger(__name__)

SETTINGS_KEY = "backup"
REQUEST_KEY = "backup_request"
SCHEDULER_KEY = "backup_scheduler"
TARGETS = ("none", "s3", "local")
PRUNE_MODES = ("script", "lifecycle")
MAX_KEEP = 1000
# Запуск, не приславший результат за это время, считается прерванным.
STALE_RUN = timedelta(hours=6)
# Бэкап «просрочен», если запуск по расписанию, наступивший больше этого времени назад, не завершился успешно.
OVERDUE_GRACE = timedelta(hours=2)
_RECIPIENT_RE = re.compile(r"^age1[02-9ac-hj-np-z]{58}$")
_NAME_RE = re.compile(r"^\d{8}T\d{6}Z(\.tar\.age)?$")
_CODE_RE = re.compile(r"^[a-z_]{1,64}$")

EDITABLE = (
    "schedule_enabled",
    "schedule",
    "timezone",
    "target",
    "keep_local",
    "keep_remote_daily",
    "keep_remote_weekly",
    "keep_remote_monthly",
    "remote_prune",
)
INT_FIELDS = ("keep_local", "keep_remote_daily", "keep_remote_weekly", "keep_remote_monthly")


@dataclass
class Problem:
    code: str
    message: str


@dataclass
class EffectiveConfig:
    values: dict[str, Any]
    env: dict[str, Any]
    sources: dict[str, str]
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _now() -> datetime:
    return datetime.now(UTC)


def _int(value: str) -> int | None:
    value = value.strip()
    return int(value) if value.isdigit() else None


def env_defaults(settings: Settings) -> dict[str, Any]:
    schedule = settings.backup_schedule.strip()
    enabled = schedule.lower() not in ("", "off", "none", "disabled")
    return {
        "schedule_enabled": enabled,
        "schedule": schedule if enabled else "0 3 * * *",
        "timezone": settings.backup_timezone.strip() or "UTC",
        "target": settings.backup_target.strip().lower() or "none",
        "keep_local": _int(settings.backup_keep_local),
        "keep_remote_daily": _int(settings.backup_keep_remote_daily),
        "keep_remote_weekly": _int(settings.backup_keep_remote_weekly),
        "keep_remote_monthly": _int(settings.backup_keep_remote_monthly),
        # Пусто — по цели: script для local, lifecycle для s3 (вычисляется в effective_config).
        "remote_prune": settings.backup_remote_prune.strip().lower() or None,
    }


def server_info(settings: Settings) -> dict[str, Any]:
    """Параметры, которые задаются только на сервере (для показа в UI; секретов среди них нет)."""
    recipient = settings.backup_encryption_recipient.strip()
    return {
        "backup_dir": settings.backup_dir,
        "min_interval_minutes": _int(settings.backup_min_interval_minutes),
        "s3": {
            "configured": bool(settings.backup_s3_endpoint.strip() and settings.backup_s3_bucket.strip()),
            "endpoint": settings.backup_s3_endpoint.strip() or None,
            "bucket": settings.backup_s3_bucket.strip() or None,
            "prefix": settings.backup_s3_prefix.strip() or None,
        },
        "local": {
            "configured": bool(settings.backup_local_target_dir.strip()),
            "dir": settings.backup_local_target_dir.strip() or None,
        },
        "encryption": {
            "configured": bool(recipient),
            "valid": bool(_RECIPIENT_RE.match(recipient)),
            # Ключ не секрет; показываем начало и конец — для сверки с ключом у администратора.
            "recipient_hint": f"{recipient[:8]}…{recipient[-6:]}" if recipient else None,
        },
    }


def validate_field(name: str, value: Any) -> None:  # noqa: PLR0912 - по ветке на поле
    """Проверка одного параметра (для UI). Ошибка — 422 с понятным сообщением."""

    def fail(message: str) -> None:
        raise AppError(message, code="invalid_backup_setting", status_code=422, details={"field": name})

    if name == "schedule_enabled":
        if not isinstance(value, bool):
            fail("Ожидается да/нет")
    elif name == "schedule":
        try:
            cron.parse(str(value))
        except cron.CronError as exc:
            fail(f"Расписание: {exc}")
    elif name == "timezone":
        try:
            ZoneInfo(str(value))
        except (ZoneInfoNotFoundError, ValueError):
            fail("Неизвестный часовой пояс")
    elif name == "target":
        if value not in TARGETS:
            fail("Цель: none, s3 или local")
    elif name in INT_FIELDS:
        low = 1 if name == "keep_local" else 0
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= MAX_KEEP:
            fail(f"Число от {low} до {MAX_KEEP}")
    elif name == "remote_prune":
        if value not in PRUNE_MODES:
            fail("Ротация: script или lifecycle")
    else:
        fail("Неизвестный параметр")


async def _setting(db: AsyncSession, key: str) -> Any:
    return (
        await db.execute(select(SystemSetting.value).where(SystemSetting.key == key))
    ).scalar_one_or_none()


async def overrides(db: AsyncSession) -> dict[str, Any]:
    value = await _setting(db, SETTINGS_KEY)
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if k in EDITABLE}


async def effective_config(db: AsyncSession, settings: Settings) -> EffectiveConfig:
    env = env_defaults(settings)
    ui = await overrides(db)
    values = {**env, **ui}
    sources = {k: ("ui" if k in ui else "env") for k in EDITABLE}
    if values["remote_prune"] is None:
        values["remote_prune"] = "script" if values["target"] == "local" else "lifecycle"
        sources["remote_prune"] = "default"
    config = EffectiveConfig(values=values, env=env, sources=sources)
    config.problems = _problems(values, settings)
    return config


def _problems(values: dict[str, Any], settings: Settings) -> list[Problem]:  # noqa: PLR0912 - по ветке на условие
    problems: list[Problem] = []
    for name in EDITABLE:
        try:
            if values[name] is None:
                raise AppError("Значение не задано или не является числом")
            validate_field(name, values[name])
        except AppError as exc:
            problems.append(Problem(f"invalid_{name}", exc.message))
    target = values["target"]
    info = server_info(settings)
    if target != "none":
        remote = [values[k] or 0 for k in ("keep_remote_daily", "keep_remote_weekly", "keep_remote_monthly")]
        if values["remote_prune"] == "script" and sum(remote) == 0:
            problems.append(
                Problem("remote_keep_empty", "Во внешнем хранилище должна храниться хотя бы одна копия")
            )
        if not info["encryption"]["configured"]:
            problems.append(
                Problem(
                    "encryption_required",
                    "Для выгрузки за пределы сервера задайте BACKUP_ENCRYPTION_RECIPIENT"
                    " (публичный ключ age)",
                )
            )
        elif not info["encryption"]["valid"]:
            problems.append(
                Problem("invalid_recipient", "BACKUP_ENCRYPTION_RECIPIENT — не публичный ключ age")
            )
    elif settings.backup_encryption_recipient.strip() and not info["encryption"]["valid"]:
        problems.append(Problem("invalid_recipient", "BACKUP_ENCRYPTION_RECIPIENT — не публичный ключ age"))
    if target == "s3" and not info["s3"]["configured"]:
        problems.append(
            Problem("s3_not_configured", "Для цели s3 задайте BACKUP_S3_ENDPOINT и BACKUP_S3_BUCKET")
        )
    if target == "local":
        local_dir = settings.backup_local_target_dir.strip()
        if not local_dir:
            problems.append(Problem("local_not_configured", "Для цели local задайте BACKUP_LOCAL_TARGET_DIR"))
        elif not local_dir.startswith("/"):
            problems.append(
                Problem("local_dir_not_absolute", "BACKUP_LOCAL_TARGET_DIR должен быть абсолютным путём")
            )
        elif local_dir.rstrip("/") == settings.backup_dir.rstrip("/"):
            problems.append(
                Problem(
                    "local_dir_same_as_backup_dir", "BACKUP_LOCAL_TARGET_DIR должен отличаться от BACKUP_DIR"
                )
            )
    if info["min_interval_minutes"] is None:
        problems.append(Problem("invalid_min_interval", "BACKUP_MIN_INTERVAL_MINUTES — целое число минут"))
    return problems


def schedule_times(config: EffectiveConfig, now: datetime) -> tuple[datetime | None, datetime | None]:
    """Предыдущий и следующий запуск по расписанию (None — расписание выключено или некорректно)."""
    values = config.values
    if not values["schedule_enabled"]:
        return None, None
    try:
        spec = cron.parse(str(values["schedule"]))
        tz = ZoneInfo(str(values["timezone"]))
    except (cron.CronError, ZoneInfoNotFoundError, ValueError):
        return None, None
    return cron.previous_fire(spec, now, tz), cron.next_fire(spec, now, tz)


async def close_stale_runs(db: AsyncSession) -> int:
    result = await db.execute(
        update(BackupRun)
        .where(BackupRun.status == BackupRunStatus.running, BackupRun.started_at < func.now() - STALE_RUN)
        .values(status=BackupRunStatus.interrupted, finished_at=func.now(), error_code="no_report")
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def _last_started(db: AsyncSession) -> datetime | None:
    value: datetime | None = (await db.execute(select(func.max(BackupRun.started_at)))).scalar_one()
    return value


async def _last_success(db: AsyncSession) -> datetime | None:
    value: datetime | None = (
        await db.execute(
            select(func.max(BackupRun.finished_at)).where(BackupRun.status == BackupRunStatus.success)
        )
    ).scalar_one()
    return value


@dataclass
class Plan:
    config: EffectiveConfig
    due: bool
    trigger: BackupTrigger | None


async def plan(db: AsyncSession, settings: Settings, *, scheduler: bool, now: datetime | None = None) -> Plan:
    """Пора ли выполнять бэкап (для скрипта на хосте). `scheduler=True` — вызов из планировщика."""
    now = now or _now()
    await close_stale_runs(db)
    if scheduler:
        await _put(db, SCHEDULER_KEY, {"seen_at": now.isoformat()}, None)
    config = await effective_config(db, settings)
    await db.commit()
    if not config.ok:
        return Plan(config, due=False, trigger=None)
    last_started = await _last_started(db)
    request = await _setting(db, REQUEST_KEY)
    if isinstance(request, dict) and _after(request.get("requested_at"), last_started):
        return Plan(config, due=True, trigger=BackupTrigger.requested)
    previous, _next = schedule_times(config, now)
    if previous is not None and (last_started is None or last_started < previous):
        return Plan(config, due=True, trigger=BackupTrigger.scheduled)
    return Plan(config, due=False, trigger=None)


def _after(value: Any, moment: datetime | None) -> bool:
    if not isinstance(value, str):
        return False
    try:
        requested = datetime.fromisoformat(value)
    except ValueError:
        return False
    return moment is None or requested > moment


async def _put(db: AsyncSession, key: str, value: Any, user_id: uuid.UUID | None) -> None:
    await db.execute(
        pg_insert(SystemSetting)
        .values(key=key, value=value, updated_by_id=user_id)
        .on_conflict_do_update(
            index_elements=[SystemSetting.key],
            set_={"value": value, "updated_by_id": user_id, "updated_at": func.now()},
        )
    )


# ---------------------------------------------------------------- изменения из UI


async def update_settings(
    db: AsyncSession,
    settings: Settings,
    actor: User,
    *,
    ip: str | None,
    changes: dict[str, Any],
    reset: list[str],
) -> None:
    policies.ensure(policies.can_manage_backup(actor))
    for name in reset:
        if name not in EDITABLE:
            raise AppError("Неизвестный параметр", code="invalid_backup_setting", status_code=422)
    for name, value in changes.items():
        validate_field(name, value)
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext('backup_settings'))"))
    before = await effective_config(db, settings)
    current = await overrides(db)
    updated = {k: v for k, v in current.items() if k not in reset}
    updated.update(changes)
    await _put(db, SETTINGS_KEY, updated, actor.id)
    after = await effective_config(db, settings)
    # Изменение из UI не должно ломать работающие настройки (например, цель, не настроенная на сервере).
    known = {p.code for p in before.problems}
    introduced = [p for p in after.problems if p.code not in known]
    if introduced:
        await db.rollback()
        raise AppError(
            "; ".join(p.message for p in introduced),
            code="backup_settings_invalid",
            status_code=422,
            details={"problems": [p.code for p in introduced]},
        )
    diff = {
        k: {"from": before.values[k], "to": after.values[k]}
        for k in EDITABLE
        if before.values[k] != after.values[k] or before.sources[k] != after.sources[k]
    }
    if not diff:
        await db.commit()
        return
    revision = (
        await db.execute(
            select(func.coalesce(func.max(ConfigRevision.revision), 0)).where(
                ConfigRevision.entity_type == "setting", ConfigRevision.entity_id == "backup"
            )
        )
    ).scalar_one()
    await db.execute(
        insert(ConfigRevision).values(
            entity_type="setting",
            entity_id="backup",
            revision=revision + 1,
            diff=diff,
            changed_by_id=actor.id,
        )
    )
    record(
        db,
        AuditAction.settings_updated,
        actor_id=actor.id,
        target_type="setting",
        target_id="backup",
        ip=ip,
        details={"changes": diff},
    )
    await db.commit()


async def request_run(db: AsyncSession, actor: User, ip: str | None) -> None:
    """«Запустить сейчас»: запрос выполнит планировщик на сервере при ближайшем опросе."""
    policies.ensure(policies.can_manage_backup(actor))
    await _put(db, REQUEST_KEY, {"requested_at": _now().isoformat(), "requested_by": str(actor.id)}, actor.id)
    record(
        db, AuditAction.backup_requested, actor_id=actor.id, target_type="setting", target_id="backup", ip=ip
    )
    await db.commit()


async def pending_request(db: AsyncSession) -> dict[str, Any] | None:
    request = await _setting(db, REQUEST_KEY)
    if not isinstance(request, dict) or not _after(request.get("requested_at"), await _last_started(db)):
        return None
    return request


async def scheduler_seen_at(db: AsyncSession) -> datetime | None:
    value = await _setting(db, SCHEDULER_KEY)
    if isinstance(value, dict) and isinstance(value.get("seen_at"), str):
        try:
            return datetime.fromisoformat(value["seen_at"])
        except ValueError:
            return None
    return None


async def last_success_at(db: AsyncSession) -> datetime | None:
    return await _last_success(db)


def is_overdue(config: EffectiveConfig, last_success: datetime | None, now: datetime | None = None) -> bool:
    now = now or _now()
    previous, _next = schedule_times(config, now - OVERDUE_GRACE)
    return previous is not None and (last_success is None or last_success < previous)


# ---------------------------------------------------------------- отчёты скрипта


async def report_start(db: AsyncSession, run_id: uuid.UUID, trigger: BackupTrigger, target: str) -> None:
    if target not in TARGETS:
        raise AppError("Некорректная цель", code="invalid_backup_report")
    db.add(BackupRun(id=run_id, status=BackupRunStatus.running, trigger=trigger, target=target))
    if trigger is BackupTrigger.requested:
        await db.execute(delete(SystemSetting).where(SystemSetting.key == REQUEST_KEY))
    await db.commit()


async def report_finish(
    db: AsyncSession,
    run_id: uuid.UUID,
    *,
    success: bool,
    error_code: str | None,
    name: str | None,
    size_bytes: int | None,
    encrypted: bool | None,
    uploaded: bool | None,
    verified: bool | None,
    pruned_local: int | None,
    pruned_remote: int | None,
) -> BackupRun:
    if name is not None and not _NAME_RE.match(name):
        raise AppError("Некорректное имя бэкапа", code="invalid_backup_report")
    if error_code is not None and not _CODE_RE.match(error_code):
        raise AppError("Некорректный код ошибки", code="invalid_backup_report")
    run = await db.get(BackupRun, run_id, with_for_update=True)
    if run is None:
        raise NotFoundError("Запуск бэкапа не найден", code="backup_run_not_found")
    run.status = BackupRunStatus.success if success else BackupRunStatus.failed
    run.finished_at = _now()
    run.error_code = error_code
    run.name = name
    run.size_bytes = size_bytes
    run.encrypted = encrypted
    run.uploaded = uploaded
    run.verified = verified
    run.pruned_local = pruned_local
    run.pruned_remote = pruned_remote
    record(
        db,
        AuditAction.backup_completed if success else AuditAction.backup_failed,
        actor_id=None,
        target_type="backup",
        target_id=run.id,
        details={
            "trigger": run.trigger.value,
            "target": run.target,
            "name": name,
            "size_bytes": size_bytes,
            "uploaded": uploaded,
            "verified": verified,
            "error_code": error_code,
        },
    )
    await db.commit()
    log.info("backup_reported", success=success, error_code=error_code, target=run.target)
    return run


async def recent_runs(db: AsyncSession, limit: int = 20) -> list[BackupRun]:
    return list(
        (await db.execute(select(BackupRun).order_by(BackupRun.started_at.desc()).limit(limit))).scalars()
    )
