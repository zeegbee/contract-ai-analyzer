"""Документы: приём загрузки, представление для API, удаление, выдача доступа.

Права проверяет API-слой через `app.security.scoped` (условие доступа в SQL); здесь — бизнес-логика.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from app.errors import AppError, ConflictError, NotFoundError, PermissionDenied
from app.ingestion.formats import DetectedFile, FileRejected, detect_and_validate, title_from_filename
from app.jobs import queue
from app.jobs.handlers.documents import EXTRACT_JOB, PURGE_JOB
from app.models.documents import Document, DocumentFragment, DocumentVersion
from app.models.enums import DocumentFormat, DocumentPermission, DocumentStatus, ExtractionStatus, UserRole
from app.models.users import DocumentAccess, User
from app.schemas.documents import AccessKind, DocumentOut, VersionOut, VersionSummaryOut
from app.security.policies import Actor, document_capabilities, is_superadmin
from app.services.audit import AuditAction, record
from app.storage import AtomicWriter, LocalStorage, StorageError

IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
# Повторы при недоступности converter: backoff 5, 10, 20, 40 с.
EXTRACT_MAX_ATTEMPTS = 5

# Код → (HTTP-статус, сообщение для пользователя).
_UPLOAD_ERRORS: dict[str, tuple[int, str]] = {
    "empty_file": (422, "Файл пустой"),
    "unsupported_file_type": (415, "Поддерживаются только DOCX, DOC и PDF с текстовым слоем"),
    "file_type_mismatch": (415, "Расширение файла не соответствует его содержимому"),
    "mime_type_mismatch": (415, "Тип файла (MIME) не соответствует его содержимому"),
    "macros_not_allowed": (415, "Документы с макросами (DOCM) не принимаются"),
    "encrypted_document": (422, "Документ защищён паролем. Снимите защиту и загрузите файл снова"),
    "corrupted_file": (422, "Файл повреждён или не читается"),
    "archive_limits_exceeded": (422, "Структура файла превышает допустимые лимиты"),
    "unsafe_archive_path": (422, "Файл содержит недопустимые пути внутри архива"),
}


class UploadRejected(AppError):
    status_code = 422
    code = "upload_rejected"
    message = "Файл не принят"


def upload_error(code: str) -> UploadRejected:
    status, message = _UPLOAD_ERRORS.get(code, (422, "Файл не принят"))
    return UploadRejected(message, code=code, status_code=status)


def too_large_error(max_mb: int) -> UploadRejected:
    return UploadRejected(
        f"Файл больше {max_mb} МБ", code="file_too_large", status_code=413, details={"max_mb": max_mb}
    )


# ---------------------------------------------------------------- приём файла


async def receive_stream(
    stream: AsyncIterator[bytes], storage: LocalStorage, key: str, *, max_bytes: int, max_mb: int
) -> AtomicWriter:
    """Потоковая запись тела запроса во временный файл с лимитом размера и SHA-256."""
    writer = storage.writer(key, max_bytes=max_bytes)
    try:
        async for chunk in stream:
            if chunk:
                writer.write(chunk)
        await run_in_threadpool(writer.close)
    except StorageError as exc:
        writer.abort()
        raise too_large_error(max_mb) from exc
    except ClientDisconnect as exc:
        writer.abort()
        raise AppError("Загрузка прервана", code="upload_interrupted") from exc
    except BaseException:
        writer.abort()
        raise
    return writer


async def validate_file(writer: AtomicWriter, filename: str, declared_mime: str | None) -> DetectedFile:
    try:
        return await run_in_threadpool(detect_and_validate, writer.tmp_path, filename, declared_mime)
    except FileRejected as exc:
        writer.abort()
        raise upload_error(exc.code) from exc


async def pending_extractions(db: AsyncSession, user_id: uuid.UUID) -> int:
    stmt = (
        select(func.count())
        .select_from(DocumentVersion)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            DocumentVersion.uploaded_by_id == user_id,
            DocumentVersion.extraction_status.in_([ExtractionStatus.pending, ExtractionStatus.processing]),
            Document.status == DocumentStatus.active,
        )
    )
    return int((await db.execute(stmt)).scalar_one())


async def find_by_idempotency_key(db: AsyncSession, owner_id: uuid.UUID, key: str) -> Document | None:
    stmt = select(Document).where(
        Document.owner_id == owner_id,
        Document.upload_idempotency_key == key,
        Document.status == DocumentStatus.active,
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def create_document(
    db: AsyncSession,
    *,
    owner: User,
    ip: str | None,
    filename: str,
    detected: DetectedFile,
    storage_key: str,
    size_bytes: int,
    sha256: str,
    idempotency_key: str | None,
) -> Document:
    """Документ + версия 1 + задача извлечения + аудит — одной транзакцией (commit — вызывающий)."""
    document = Document(
        id=uuid.uuid4(),
        owner_id=owner.id,
        title=title_from_filename(filename),
        status=DocumentStatus.active,
        upload_idempotency_key=idempotency_key,
    )
    version = DocumentVersion(
        id=uuid.uuid4(),
        document_id=document.id,
        version_no=1,
        original_filename=filename,
        detected_format=DocumentFormat(detected.format),
        mime_type=detected.mime_type,
        size_bytes=size_bytes,
        sha256=sha256,
        storage_key=storage_key,
        extraction_status=ExtractionStatus.pending,
        warnings=detected.warnings,
        uploaded_by_id=owner.id,
    )
    db.add(document)
    await db.flush()
    db.add(version)
    await db.flush()
    document.current_version_id = version.id
    await queue.enqueue(
        db,
        EXTRACT_JOB,
        {"version_id": str(version.id)},
        priority=50,
        max_attempts=EXTRACT_MAX_ATTEMPTS,
        dedup_key=f"extract:{version.id}",
    )
    # Название и имя файла в аудит не пишутся (могут быть конфиденциальными).
    record(
        db,
        AuditAction.document_uploaded,
        actor_id=owner.id,
        target_type="document",
        target_id=document.id,
        ip=ip,
        details={
            "version_id": str(version.id),
            "format": detected.format,
            "size_bytes": size_bytes,
            "sha256": sha256,
        },
    )
    return document


async def retry_extraction(db: AsyncSession, actor: User, ip: str | None, version: DocumentVersion) -> None:
    if version.extraction_status is not ExtractionStatus.failed:
        raise ConflictError("Повторить можно только неудавшееся извлечение", code="extraction_not_failed")
    version.extraction_status = ExtractionStatus.pending
    version.extraction_error_code = None
    await queue.enqueue(
        db,
        EXTRACT_JOB,
        {"version_id": str(version.id)},
        priority=50,
        max_attempts=EXTRACT_MAX_ATTEMPTS,
        dedup_key=f"extract:{version.id}",
    )
    record(
        db,
        AuditAction.document_extraction_retried,
        actor_id=actor.id,
        target_type="document",
        target_id=version.document_id,
        ip=ip,
        details={"version_id": str(version.id)},
    )


async def mark_for_deletion(db: AsyncSession, actor: User, ip: str | None, document: Document) -> None:
    """Документ сразу исчезает из выдачи; файлы и строки удаляет задача purge_document."""
    shas = (
        await db.execute(select(DocumentVersion.sha256).where(DocumentVersion.document_id == document.id))
    ).scalars()
    document.status = DocumentStatus.deleting
    document.deleted_at = datetime.now(UTC)
    await queue.enqueue(
        db, PURGE_JOB, {"document_id": str(document.id)}, priority=200, dedup_key=f"purge:{document.id}"
    )
    record(
        db,
        AuditAction.document_deleted,
        actor_id=actor.id,
        target_type="document",
        target_id=document.id,
        ip=ip,
        details={"sha256": list(shas), "owner_id": str(document.owner_id)},
    )


# ---------------------------------------------------------------- представление


def version_summary(version: DocumentVersion) -> VersionSummaryOut:
    return VersionSummaryOut(
        id=version.id,
        version_no=version.version_no,
        original_filename=version.original_filename,
        detected_format=version.detected_format,
        size_bytes=version.size_bytes,
        extraction_status=version.extraction_status,
        extraction_error_code=version.extraction_error_code,
        page_count=version.page_count,
        page_source=version.page_source,
        created_at=version.created_at,
    )


async def version_detail(db: AsyncSession, version: DocumentVersion) -> VersionOut:
    count = (
        await db.execute(
            select(func.count())
            .select_from(DocumentFragment)
            .where(DocumentFragment.version_id == version.id)
        )
    ).scalar_one()
    return VersionOut(
        **version_summary(version).model_dump(),
        mime_type=version.mime_type,
        sha256=version.sha256,
        extractor_version=version.extractor_version,
        warnings=list(version.warnings or []),
        fragment_count=int(count),
        converted_from_doc=version.converted_storage_key is not None,
        updated_at=version.updated_at,
    )


async def document_views(db: AsyncSession, actor: User, documents: Sequence[Document]) -> list[DocumentOut]:
    if not documents:
        return []
    doc_ids = [d.id for d in documents]
    owners = dict(
        (
            await db.execute(
                select(User.id, User.display_name).where(User.id.in_({d.owner_id for d in documents}))
            )
        ).all()
    )
    version_ids = [d.current_version_id for d in documents if d.current_version_id]
    versions = {
        v.id: v
        for v in (
            await db.execute(select(DocumentVersion).where(DocumentVersion.id.in_(version_ids)))
        ).scalars()
    }
    grants: dict[uuid.UUID, DocumentPermission] = dict(
        (
            await db.execute(
                select(DocumentAccess.document_id, DocumentAccess.permission).where(
                    DocumentAccess.document_id.in_(doc_ids), DocumentAccess.user_id == actor.id
                )
            )
        ).all()
    )
    result = []
    for doc in documents:
        granted = grants.get(doc.id)
        access: AccessKind
        if doc.owner_id == actor.id:
            access = "owner"
        elif granted is not None:
            access = "analyze" if granted is DocumentPermission.analyze else "view"
        else:
            access = "system" if is_superadmin(actor) else "view"
        version = versions.get(doc.current_version_id) if doc.current_version_id else None
        result.append(
            DocumentOut(
                id=doc.id,
                title=doc.title,
                owner_id=doc.owner_id,
                owner_display_name=owners.get(doc.owner_id, ""),
                status=doc.status,
                current_version_id=doc.current_version_id,
                current_version=version_summary(version) if version else None,
                created_at=doc.created_at,
                is_owner=doc.owner_id == actor.id,
                access=access,
                **document_capabilities(actor, doc.owner_id, granted),
            )
        )
    return result


# ---------------------------------------------------------------- доступ к документу


def ensure_owner_can_share(actor: Actor, document: Document) -> None:
    caps = document_capabilities(actor, document.owner_id, None)
    if not caps["can_share"]:
        raise PermissionDenied("Выдавать доступ может только владелец документа", code="not_document_owner")


async def grant_access(
    db: AsyncSession,
    *,
    actor: User,
    ip: str | None,
    document: Document,
    email: str,
    permission: DocumentPermission,
) -> None:
    target = (await db.execute(select(User).where(User.email == email.strip().lower()))).scalar_one_or_none()
    if target is None:
        raise NotFoundError("Пользователь с таким e-mail не найден", code="user_not_found")
    if target.id == document.owner_id:
        raise AppError("Владелец уже имеет полный доступ", code="cannot_grant_owner", status_code=422)
    if permission is DocumentPermission.analyze and target.role is UserRole.viewer:
        raise AppError(
            "Наблюдатель может получить только доступ на просмотр",
            code="viewer_cannot_analyze",
            status_code=422,
        )
    stmt = pg_insert(DocumentAccess).values(
        id=uuid.uuid4(),
        document_id=document.id,
        user_id=target.id,
        permission=permission,
        granted_by_id=actor.id,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[DocumentAccess.document_id, DocumentAccess.user_id],
        set_={"permission": permission, "granted_by_id": actor.id},
    )
    await db.execute(stmt)
    record(
        db,
        AuditAction.document_access_granted,
        actor_id=actor.id,
        target_type="document",
        target_id=document.id,
        ip=ip,
        details={"user_id": str(target.id), "permission": permission.value},
    )


async def revoke_access(
    db: AsyncSession, actor: User, ip: str | None, document: Document, user_id: uuid.UUID
) -> None:
    access = (
        await db.execute(
            select(DocumentAccess).where(
                DocumentAccess.document_id == document.id, DocumentAccess.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if access is None:
        raise NotFoundError("Доступ не найден", code="access_not_found")
    await db.delete(access)
    record(
        db,
        AuditAction.document_access_revoked,
        actor_id=actor.id,
        target_type="document",
        target_id=document.id,
        ip=ip,
        details={"user_id": str(user_id)},
    )


def access_rows_query(document_id: uuid.UUID) -> Any:
    return (
        select(DocumentAccess, User)
        .join(User, User.id == DocumentAccess.user_id)
        .where(DocumentAccess.document_id == document_id)
        .order_by(DocumentAccess.created_at)
    )
