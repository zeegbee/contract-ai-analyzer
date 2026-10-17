"""Экспорт отчётов: создание (задача `build_export`), формирование файла, скачивание, истечение срока.

Права: экспорт создаёт и скачивает любой, у кого есть доступ `view` к документу анализа (включая
Viewer, ARCHITECTURE §6.1); проверка — scoped-запросами через анализ → документ.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.errors import AppError, ConflictError
from app.exports.content import build_report
from app.exports.render import RENDERERS
from app.jobs import queue
from app.logging import get_logger
from app.models.analyses import Analysis
from app.models.documents import Document
from app.models.enums import AnalysisStatus, DocumentStatus, ExportFormat, ExportStatus
from app.models.system import Export
from app.models.users import User
from app.services.audit import AuditAction, record
from app.storage import LocalStorage

log = get_logger(__name__)

BUILD_EXPORT_JOB = "build_export"
EXPORTABLE = (AnalysisStatus.completed, AnalysisStatus.completed_with_warnings)
EXTENSIONS = {
    ExportFormat.pdf: "pdf",
    ExportFormat.docx: "docx",
    ExportFormat.csv: "csv",
    ExportFormat.xlsx: "xlsx",
}


class ExportExpired(AppError):
    status_code = 410
    code = "export_expired"
    message = "Срок хранения файла экспорта истёк — сформируйте его заново"


def _now() -> datetime:
    return datetime.now(UTC)


def download_name(export: Export) -> str:
    # Название договора в имя файла не попадает (может быть конфиденциальным и небезопасным для заголовка).
    return f"contract-analysis-{str(export.analysis_id)[:8]}.{EXTENSIONS[export.format]}"


async def create_export(
    db: AsyncSession, settings: Settings, user: User, *, ip: str | None, analysis: Analysis, fmt: ExportFormat
) -> Export:
    if analysis.status not in EXPORTABLE:
        raise ConflictError("Экспорт доступен после завершения анализа", code="analysis_not_finished")
    pending = (
        await db.execute(
            select(func.count())
            .select_from(Export)
            .where(Export.requested_by_id == user.id, Export.status == ExportStatus.queued)
        )
    ).scalar_one()
    if pending >= settings.max_pending_exports_per_user:
        raise AppError(
            "Слишком много экспортов в очереди. Дождитесь их готовности.",
            code="too_many_pending_exports",
            status_code=429,
        )
    export = Export(
        id=uuid.uuid4(),
        analysis_id=analysis.id,
        requested_by_id=user.id,
        format=fmt,
        status=ExportStatus.queued,
    )
    db.add(export)
    await db.flush()
    await queue.enqueue(
        db,
        BUILD_EXPORT_JOB,
        {"export_id": str(export.id)},
        priority=40,
        max_attempts=3,
        dedup_key=f"export:{export.id}",
    )
    record(
        db,
        AuditAction.export_created,
        actor_id=user.id,
        target_type="export",
        target_id=export.id,
        ip=ip,
        details={"analysis_id": str(analysis.id), "format": fmt.value},
    )
    await db.commit()
    await db.refresh(export)
    return export


async def build_export(
    session_factory: async_sessionmaker[AsyncSession],
    storage: LocalStorage,
    settings: Settings,
    export_id: uuid.UUID,
) -> None:
    """Формирует файл. Повторный запуск для готового экспорта ничего не делает."""
    async with session_factory() as db:
        row = (
            await db.execute(
                select(Export, Analysis.status, Document.status)
                .join(Analysis, Analysis.id == Export.analysis_id)
                .join(Document, Document.id == Analysis.document_id)
                .where(Export.id == export_id)
            )
        ).one_or_none()
        if row is None:
            return
        export, analysis_status, document_status = row
        if export.status is not ExportStatus.queued:
            return
        if document_status is not DocumentStatus.active or analysis_status not in EXPORTABLE:
            export.status = ExportStatus.failed
            export.error_code = "analysis_unavailable"
            await db.commit()
            return
        report = await build_report(db, export.analysis_id)
        fmt = export.format
        await db.commit()
    data = await asyncio.to_thread(RENDERERS[fmt.value], report)
    key = storage.new_key("exports", f".{EXTENSIONS[fmt]}")
    await asyncio.to_thread(storage.write_bytes, key, data)
    async with session_factory() as db:
        updated = (
            await db.execute(
                update(Export)
                .where(Export.id == export_id, Export.status == ExportStatus.queued)
                .values(
                    status=ExportStatus.ready,
                    storage_key=key,
                    size_bytes=len(data),
                    expires_at=_now() + timedelta(hours=settings.export_ttl_hours),
                    error_code=None,
                )
                .returning(Export.id)
            )
        ).first()
        await db.commit()
    if updated is None:  # экспорт удалён вместе с документом во время формирования
        await asyncio.to_thread(storage.delete, key)
        return
    log.info("export_ready", export_id=str(export_id), format=fmt.value, size=len(data))


async def mark_failed(
    session_factory: async_sessionmaker[AsyncSession], export_id: uuid.UUID, code: str
) -> None:
    async with session_factory() as db:
        await db.execute(
            update(Export)
            .where(Export.id == export_id, Export.status == ExportStatus.queued)
            .values(status=ExportStatus.failed, error_code=code[:64])
        )
        await db.commit()


async def expire_exports(session_factory: async_sessionmaker[AsyncSession], storage: LocalStorage) -> int:
    """Удаляет файлы экспортов с истёкшим сроком (строка остаётся со статусом `expired`)."""
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(Export.id, Export.storage_key)
                .where(Export.status == ExportStatus.ready, Export.expires_at < func.now())
                .with_for_update(skip_locked=True)
            )
        ).all()
        if rows:
            await db.execute(
                update(Export)
                .where(Export.id.in_([r[0] for r in rows]))
                .values(status=ExportStatus.expired, storage_key=None)
            )
        await db.commit()
    for _id, key in rows:
        if key:
            await asyncio.to_thread(storage.delete, key)
    return len(rows)


def ensure_downloadable(export: Export) -> str:
    if export.status is ExportStatus.expired or (
        export.expires_at is not None and export.expires_at < _now()
    ):
        raise ExportExpired()
    if export.status is not ExportStatus.ready or not export.storage_key:
        raise ConflictError("Файл экспорта ещё не готов", code="export_not_ready")
    return export.storage_key
