"""Политика хранения документов (`retention_days`, ARCHITECTURE §17).

По умолчанию выключена. Superadmin задаёт срок N дней; worker (housekeeping, раз в минуту) удаляет
документы, с которыми ничего не происходило дольше N дней:
- документ загружен раньше срока;
- нет версий, загруженных позже срока, и анализов, запущенных позже срока;
- нет активного анализа (в очереди, выполняется, проверка, отмена) — такой документ ждёт следующего прохода.

Удаление — тот же путь, что и ручное (`mark_for_deletion` → задача `purge_document`): файлы и строки
удаляются, в аудите `document.deleted` без автора с `reason: retention`. Включение и сокращение срока
требуют явного подтверждения (сервер возвращает число документов, которые будут удалены сразу).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import exists, func, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.errors import AppError, ConflictError
from app.logging import get_logger
from app.models.analyses import Analysis
from app.models.documents import Document, DocumentVersion
from app.models.enums import AnalysisStatus, DocumentStatus
from app.models.system import ConfigRevision, SystemSetting
from app.models.users import User
from app.security import policies
from app.services.audit import AuditAction, record
from app.services.documents import mark_for_deletion

log = get_logger(__name__)

RETENTION_KEY = "document_retention"
MIN_DAYS = 1
MAX_DAYS = 3650
# Сколько документов удаляется за один проход housekeeping (раз в минуту).
BATCH_SIZE = 100
_ACTIVE_ANALYSIS = (
    AnalysisStatus.queued,
    AnalysisStatus.running,
    AnalysisStatus.verifying,
    AnalysisStatus.cancelling,
)


@dataclass(frozen=True)
class RetentionState:
    retention_days: int | None
    updated_at: datetime | None
    updated_by_id: uuid.UUID | None


def _now() -> datetime:
    return datetime.now(UTC)


async def get_state(db: AsyncSession) -> RetentionState:
    row = (
        await db.execute(
            select(SystemSetting.value, SystemSetting.updated_at, SystemSetting.updated_by_id).where(
                SystemSetting.key == RETENTION_KEY
            )
        )
    ).first()
    if row is None:
        return RetentionState(None, None, None)
    value: Any = row[0]
    days = value.get("days") if isinstance(value, dict) else None
    valid = isinstance(days, int) and not isinstance(days, bool) and MIN_DAYS <= days <= MAX_DAYS
    return RetentionState(days if valid else None, row[1], row[2])


def _eligible_conditions(days: int, now: datetime | None = None) -> list[Any]:
    """Условия WHERE для документов, которые политика хранения удалит при сроке `days`."""
    cutoff = (now or _now()) - timedelta(days=days)
    recent_version = exists().where(
        DocumentVersion.document_id == Document.id, DocumentVersion.created_at >= cutoff
    )
    recent_analysis = exists().where(Analysis.document_id == Document.id, Analysis.created_at >= cutoff)
    active_analysis = exists().where(
        Analysis.document_id == Document.id, Analysis.status.in_(_ACTIVE_ANALYSIS)
    )
    return [
        Document.status == DocumentStatus.active,
        Document.created_at < cutoff,
        ~recent_version,
        ~recent_analysis,
        ~active_analysis,
    ]


async def count_eligible(db: AsyncSession, days: int) -> int:
    stmt = select(func.count()).select_from(Document).where(*_eligible_conditions(days))
    return int((await db.execute(stmt)).scalar_one())


def validate_days(days: int | None) -> None:
    if days is not None and not MIN_DAYS <= days <= MAX_DAYS:
        raise AppError(
            f"Срок хранения — от {MIN_DAYS} до {MAX_DAYS} дней",
            code="invalid_retention_days",
            status_code=422,
        )


async def update(
    db: AsyncSession, actor: User, ip: str | None, *, days: int | None, confirm: bool
) -> RetentionState:
    """Изменение политики (только Superadmin). Включение и сокращение срока — только с `confirm`."""
    policies.ensure(policies.can_manage_retention(actor))
    validate_days(days)
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext('document_retention'))"))
    before = (await get_state(db)).retention_days
    if days == before:
        await db.commit()
        return await get_state(db)
    stricter = days is not None and (before is None or days < before)
    if days is not None and stricter and not confirm:
        eligible = await count_eligible(db, days)
        await db.commit()
        raise ConflictError(
            "Подтвердите автоматическое удаление документов",
            code="retention_confirmation_required",
            details={"eligible": eligible},
        )
    await db.execute(
        pg_insert(SystemSetting)
        .values(key=RETENTION_KEY, value={"days": days}, updated_by_id=actor.id)
        .on_conflict_do_update(
            index_elements=[SystemSetting.key],
            set_={"value": {"days": days}, "updated_by_id": actor.id, "updated_at": func.now()},
        )
    )
    diff = {"retention_days": {"from": before, "to": days}}
    revision = (
        await db.execute(
            select(func.coalesce(func.max(ConfigRevision.revision), 0)).where(
                ConfigRevision.entity_type == "setting", ConfigRevision.entity_id == "retention"
            )
        )
    ).scalar_one()
    await db.execute(
        insert(ConfigRevision).values(
            entity_type="setting",
            entity_id="retention",
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
        target_id="retention",
        ip=ip,
        details={"changes": diff},
    )
    await db.commit()
    log.info("retention_updated", retention_days=days)
    return await get_state(db)


async def apply_retention(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """Один проход политики хранения (вызывается из housekeeping worker'а). Возвращает число документов."""
    async with session_factory() as db:
        days = (await get_state(db)).retention_days
        if days is None:
            await db.commit()
            return 0
        # Условия проверяются в том же запросе, что и блокировка строк (не «сначала выбрать, потом удалить»).
        stmt = (
            select(Document)
            .where(*_eligible_conditions(days))
            .order_by(Document.created_at)
            .limit(BATCH_SIZE)
            .with_for_update(of=Document, skip_locked=True)
        )
        documents = list((await db.execute(stmt)).scalars())
        for document in documents:
            await mark_for_deletion(
                db, None, None, document, extra={"reason": "retention", "retention_days": days}
            )
        await db.commit()
    if documents:
        log.info("retention_documents_deleted", count=len(documents), retention_days=days)
    return len(documents)
