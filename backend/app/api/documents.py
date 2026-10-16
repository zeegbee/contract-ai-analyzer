"""API документов: загрузка, список, карточка, текст (страницы/фрагменты), оригинал, удаление, доступ.

Каждый объект загружается через `app.security.scoped` — условие доступа в SQL, чужое → 404.
Версия, страницы и фрагменты всегда проверяются через документ из URL.
"""

from __future__ import annotations

import uuid
from typing import Annotated
from urllib.parse import quote, unquote

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AppSettings, Auth, AuthContext, Db
from app.errors import AppError, NotFoundError, PermissionDenied
from app.ingestion.formats import EXTENSIONS, extension_of, sanitize_filename
from app.models.documents import Document, DocumentFragment, DocumentPage, DocumentVersion
from app.schemas.documents import (
    AccessGrantIn,
    AccessListOut,
    AccessOut,
    DeletionOut,
    DocumentDetailOut,
    DocumentListOut,
    FragmentListOut,
    FragmentOut,
    PageListOut,
    PageOut,
    UploadLimitsOut,
    VersionOut,
)
from app.security.policies import can_upload_documents
from app.security.scoped import (
    DocumentAction,
    get_document_for,
    get_version_for,
    is_superadmin_override,
    listed_documents_query,
)
from app.services import documents as svc
from app.services.audit import AuditAction, record
from app.services.users import decode_cursor, encode_cursor
from app.storage import LocalStorage

router = APIRouter(tags=["documents"])

MIB = 1024 * 1024


def _storage(request: Request) -> LocalStorage:
    storage: LocalStorage = request.app.state.storage
    return storage


async def _audit_superadmin_access(db: AsyncSession, auth: AuthContext, document: Document, via: str) -> None:
    if await is_superadmin_override(db, auth.user, document):
        record(
            db,
            AuditAction.document_accessed_by_superadmin,
            actor_id=auth.user.id,
            target_type="document",
            target_id=document.id,
            ip=auth.ip,
            details={"via": via},
        )
        await db.commit()


async def _detail(db: AsyncSession, auth: AuthContext, document: Document) -> DocumentDetailOut:
    [base] = await svc.document_views(db, auth.user, [document])
    versions = list(
        (
            await db.execute(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == document.id)
                .order_by(DocumentVersion.version_no.desc())
            )
        ).scalars()
    )
    current = next((v for v in versions if v.id == document.current_version_id), None)
    return DocumentDetailOut(
        **base.model_dump(),
        current_version_detail=await svc.version_detail(db, current) if current else None,
        versions=[svc.version_summary(v) for v in versions],
    )


# ---------------------------------------------------------------- список и загрузка


@router.get("/documents")
async def list_documents(
    auth: Auth,
    db: Db,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> DocumentListOut:
    stmt = listed_documents_query(auth.user)
    if cursor:
        created, doc_id = decode_cursor(cursor)
        stmt = stmt.where(
            or_(Document.created_at < created, and_(Document.created_at == created, Document.id < doc_id))
        )
    stmt = stmt.order_by(Document.created_at.desc(), Document.id.desc()).limit(limit + 1)
    rows = list((await db.execute(stmt)).scalars())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id)
    return DocumentListOut(items=await svc.document_views(db, auth.user, rows), next_cursor=next_cursor)


@router.post("/documents", status_code=201)
async def upload_document(
    request: Request,
    response: Response,
    auth: Auth,
    db: Db,
    settings: AppSettings,
    x_file_name: Annotated[str | None, Header(max_length=1024)] = None,
    content_type: Annotated[str | None, Header(max_length=200)] = None,
    idempotency_key: Annotated[str | None, Header(max_length=128)] = None,
) -> DocumentDetailOut:
    """Тело запроса — сам файл; имя — в `X-File-Name` (percent-encoded UTF-8)."""
    if not can_upload_documents(auth.user):
        raise PermissionDenied()
    if idempotency_key is not None and not svc.IDEMPOTENCY_KEY_RE.match(idempotency_key):
        raise AppError("Некорректный Idempotency-Key", code="invalid_idempotency_key")
    if idempotency_key:
        existing = await svc.find_by_idempotency_key(db, auth.user.id, idempotency_key)
        if existing is not None:
            response.status_code = 200
            response.headers["Idempotent-Replayed"] = "true"
            return await _detail(db, auth, existing)

    filename = sanitize_filename(unquote(x_file_name or "", errors="replace"))
    if not filename:
        raise AppError("Не указано имя файла", code="file_name_required")
    if extension_of(filename) not in EXTENSIONS:
        raise svc.upload_error("unsupported_file_type")
    max_bytes = settings.max_upload_mb * MIB
    declared_length = request.headers.get("content-length")
    if declared_length and declared_length.isdigit() and int(declared_length) > max_bytes:
        raise svc.too_large_error(settings.max_upload_mb)
    if await svc.pending_extractions(db, auth.user.id) >= settings.max_pending_extractions_per_user:
        raise AppError(
            "Слишком много документов ожидают обработки. Дождитесь завершения и повторите",
            code="too_many_pending_extractions",
            status_code=429,
        )
    # Транзакцию (только чтение) завершаем, чтобы не держать её открытой на время передачи файла.
    # commit, а не rollback: rollback «просрочил» бы загруженного пользователя.
    await db.commit()

    storage = _storage(request)
    key = storage.new_key("originals")
    writer = await svc.receive_stream(
        request.stream(), storage, key, max_bytes=max_bytes, max_mb=settings.max_upload_mb
    )
    detected = await svc.validate_file(writer, filename, content_type)
    writer.commit()
    try:
        document = await svc.create_document(
            db,
            owner=auth.user,
            ip=auth.ip,
            filename=filename,
            detected=detected,
            storage_key=key,
            size_bytes=writer.size,
            sha256=writer.sha256,
            idempotency_key=idempotency_key,
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        storage.delete(key)
        # Параллельный запрос с тем же Idempotency-Key успел первым.
        existing = (
            await svc.find_by_idempotency_key(db, auth.user.id, idempotency_key) if idempotency_key else None
        )
        if existing is None:
            raise
        response.status_code = 200
        response.headers["Idempotent-Replayed"] = "true"
        return await _detail(db, auth, existing)
    except BaseException:
        storage.delete(key)
        raise
    await db.refresh(document)
    return await _detail(db, auth, document)


@router.get("/documents/upload-limits")
async def upload_limits(auth: Auth, settings: AppSettings) -> UploadLimitsOut:
    """Лимиты для подсказок в UI; сервер проверяет их сам при загрузке."""
    return UploadLimitsOut(
        max_upload_mb=settings.max_upload_mb,
        max_pages=settings.max_pages,
        extensions=sorted(EXTENSIONS),
        can_upload=can_upload_documents(auth.user),
    )


# ---------------------------------------------------------------- документ


@router.get("/documents/{document_id}")
async def get_document(document_id: uuid.UUID, auth: Auth, db: Db) -> DocumentDetailOut:
    document = await get_document_for(db, auth.user, document_id, DocumentAction.view)
    await _audit_superadmin_access(db, auth, document, "document")
    return await _detail(db, auth, document)


@router.delete("/documents/{document_id}", status_code=202)
async def delete_document(document_id: uuid.UUID, auth: Auth, db: Db) -> DeletionOut:
    document = await get_document_for(db, auth.user, document_id, DocumentAction.manage)
    await svc.mark_for_deletion(db, auth.user, auth.ip, document)
    await db.commit()
    return DeletionOut(id=document.id, status=document.status)


# ---------------------------------------------------------------- версия, текст, файл


@router.get("/documents/{document_id}/versions/{version_id}")
async def get_version(document_id: uuid.UUID, version_id: uuid.UUID, auth: Auth, db: Db) -> VersionOut:
    document, version = await get_version_for(db, auth.user, document_id, version_id)
    await _audit_superadmin_access(db, auth, document, "version")
    return await svc.version_detail(db, version)


@router.post("/documents/{document_id}/versions/{version_id}/retry-extraction", status_code=202)
async def retry_extraction(document_id: uuid.UUID, version_id: uuid.UUID, auth: Auth, db: Db) -> VersionOut:
    _, version = await get_version_for(db, auth.user, document_id, version_id, DocumentAction.manage)
    await svc.retry_extraction(db, auth.user, auth.ip, version)
    await db.commit()
    await db.refresh(version)
    return await svc.version_detail(db, version)


@router.get("/documents/{document_id}/versions/{version_id}/pages")
async def list_pages(document_id: uuid.UUID, version_id: uuid.UUID, auth: Auth, db: Db) -> PageListOut:
    document, version = await get_version_for(db, auth.user, document_id, version_id)
    await _audit_superadmin_access(db, auth, document, "pages")
    pages = (
        await db.execute(
            select(DocumentPage).where(DocumentPage.version_id == version.id).order_by(DocumentPage.page_no)
        )
    ).scalars()
    return PageListOut(
        items=[
            PageOut(page_no=p.page_no, char_start=p.char_start, char_end=p.char_end, source=p.source)
            for p in pages
        ],
        page_source=version.page_source,
        page_count=version.page_count,
    )


@router.get("/documents/{document_id}/versions/{version_id}/fragments")
async def list_fragments(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    auth: Auth,
    db: Db,
    page: Annotated[int | None, Query(ge=1, le=100_000)] = None,
    after_seq: Annotated[int | None, Query(ge=-1)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> FragmentListOut:
    document, version = await get_version_for(db, auth.user, document_id, version_id)
    await _audit_superadmin_access(db, auth, document, "fragments")
    conditions = [DocumentFragment.version_id == version.id]
    if page is not None:
        conditions += [DocumentFragment.page_start <= page, DocumentFragment.page_end >= page]
    total = (
        await db.execute(select(func.count()).select_from(DocumentFragment).where(*conditions))
    ).scalar_one()
    stmt = select(DocumentFragment).where(*conditions)
    if after_seq is not None:
        stmt = stmt.where(DocumentFragment.seq > after_seq)
    rows = list((await db.execute(stmt.order_by(DocumentFragment.seq).limit(limit + 1))).scalars())
    next_after = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_after = rows[-1].seq
    return FragmentListOut(
        items=[
            FragmentOut(
                id=f.id,
                seq=f.seq,
                fragment_key=f.fragment_key,
                kind=f.kind,
                text=f.text,
                char_start=f.char_start,
                char_end=f.char_end,
                page_start=f.page_start,
                page_end=f.page_end,
                clause_ref=f.clause_ref,
                heading_path=[str(h) for h in f.heading_path],
                table_ref=f.table_ref,
                meta=f.meta,
            )
            for f in rows
        ],
        next_after_seq=next_after,
        total=int(total),
    )


def _content_disposition(filename: str) -> str:
    ascii_name = filename.encode("ascii", "ignore").decode().replace('"', "").replace("\\", "").strip()
    ascii_name = ascii_name if ascii_name and not ascii_name.startswith(".") else "document"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


@router.get("/documents/{document_id}/versions/{version_id}/file")
async def download_original(
    request: Request, document_id: uuid.UUID, version_id: uuid.UUID, auth: Auth, db: Db
) -> FileResponse:
    document, version = await get_version_for(db, auth.user, document_id, version_id)
    await _audit_superadmin_access(db, auth, document, "file")
    storage = _storage(request)
    if not storage.exists(version.storage_key):
        raise NotFoundError("Файл не найден", code="file_not_found")
    record(
        db,
        AuditAction.document_file_downloaded,
        actor_id=auth.user.id,
        target_type="document",
        target_id=document.id,
        ip=auth.ip,
        details={"version_id": str(version.id)},
    )
    await db.commit()
    return FileResponse(
        storage.local_path(version.storage_key),
        media_type=version.mime_type,
        headers={
            "Content-Disposition": _content_disposition(version.original_filename),
            "X-Content-Type-Options": "nosniff",
        },
    )


# ---------------------------------------------------------------- доступ


@router.get("/documents/{document_id}/access")
async def list_access(document_id: uuid.UUID, auth: Auth, db: Db) -> AccessListOut:
    document = await get_document_for(db, auth.user, document_id, DocumentAction.manage)
    svc.ensure_owner_can_share(auth.user, document)
    rows = (await db.execute(svc.access_rows_query(document.id))).all()
    return AccessListOut(
        items=[
            AccessOut(
                user_id=user.id,
                email=user.email,
                display_name=user.display_name,
                role=user.role,
                permission=access.permission,
                granted_at=access.created_at,
            )
            for access, user in rows
        ]
    )


@router.put("/documents/{document_id}/access")
async def grant_access(document_id: uuid.UUID, body: AccessGrantIn, auth: Auth, db: Db) -> AccessListOut:
    document = await get_document_for(db, auth.user, document_id, DocumentAction.manage)
    svc.ensure_owner_can_share(auth.user, document)
    await svc.grant_access(
        db, actor=auth.user, ip=auth.ip, document=document, email=body.email, permission=body.permission
    )
    await db.commit()
    return await list_access(document_id, auth, db)


@router.delete("/documents/{document_id}/access/{user_id}")
async def revoke_access(document_id: uuid.UUID, user_id: uuid.UUID, auth: Auth, db: Db) -> AccessListOut:
    document = await get_document_for(db, auth.user, document_id, DocumentAction.manage)
    svc.ensure_owner_can_share(auth.user, document)
    await svc.revoke_access(db, auth.user, auth.ip, document, user_id)
    await db.commit()
    return await list_access(document_id, auth, db)
