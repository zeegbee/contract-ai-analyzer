"""Задачи документов: извлечение текста и окончательное удаление."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ingestion.pipeline import ExtractionPipeline, RetryLater
from app.jobs.registry import JobContext, JobError, register
from app.logging import get_logger
from app.models.analyses import Analysis
from app.models.documents import Document, DocumentVersion
from app.models.enums import DocumentStatus
from app.models.system import Export
from app.storage import LocalStorage

log = get_logger(__name__)

EXTRACT_JOB = "extract_document"
PURGE_JOB = "purge_document"


def _uuid(payload: dict[str, Any], key: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(payload[key]))
    except (KeyError, ValueError) as exc:
        raise JobError("invalid_payload", retryable=False) from exc


@register(EXTRACT_JOB)
async def extract_document(ctx: JobContext, payload: dict[str, Any]) -> None:
    pipeline = ExtractionPipeline(ctx.settings, ctx.session_factory, ctx.storage)
    try:
        await pipeline.run(_uuid(payload, "version_id"), last_attempt=ctx.is_last_attempt)
    except RetryLater as exc:
        raise JobError(exc.code, retryable=True) from exc


async def purge_document_files_and_rows(
    session_factory: async_sessionmaker[AsyncSession], storage: LocalStorage, document_id: uuid.UUID
) -> bool:
    """Удаляет файлы документа, затем строки (каскадом: версии, страницы, фрагменты, анализы, экспорты).

    Работает только для документа в статусе `deleting`. Повторный запуск безопасен.
    """
    async with session_factory() as db:
        status = (
            await db.execute(select(Document.status).where(Document.id == document_id))
        ).scalar_one_or_none()
        if status is not DocumentStatus.deleting:
            return False
        version_rows = (
            await db.execute(
                select(
                    DocumentVersion.storage_key,
                    DocumentVersion.converted_storage_key,
                    DocumentVersion.canonical_text_storage_key,
                ).where(DocumentVersion.document_id == document_id)
            )
        ).all()
        export_keys = (
            await db.execute(
                select(Export.storage_key)
                .join(Analysis, Analysis.id == Export.analysis_id)
                .where(Analysis.document_id == document_id)
            )
        ).scalars()
        keys = [k for row in version_rows for k in row if k] + [k for k in export_keys if k]
    for key in keys:
        await asyncio.to_thread(storage.delete, key)
    async with session_factory() as db:
        await db.execute(
            delete(Document).where(Document.id == document_id, Document.status == DocumentStatus.deleting)
        )
        await db.commit()
    log.info("document_purged", document_id=str(document_id), files=len(keys))
    return True


@register(PURGE_JOB)
async def purge_document(ctx: JobContext, payload: dict[str, Any]) -> None:
    await purge_document_files_and_rows(ctx.session_factory, ctx.storage, _uuid(payload, "document_id"))
