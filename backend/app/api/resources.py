"""Чтение метаданных документов, анализов и экспортов с проверкой доступа.

Загрузка, извлечение, запуск анализа и экспорт появятся на этапах 04–07; здесь — только чтение,
чтобы права доступа (scoped-запросы, 404 для чужих объектов) действовали с самого начала.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Auth, AuthContext, Db
from app.models.documents import Document
from app.models.enums import AnalysisStatus, DocumentStatus, ExportFormat, ExportStatus
from app.security.scoped import (
    DocumentAction,
    get_analysis_for,
    get_document_for,
    get_export_for,
    is_superadmin_override,
    listed_documents_query,
)
from app.services.audit import AuditAction, record
from app.services.users import decode_cursor, encode_cursor

router = APIRouter(tags=["documents"])


class DocumentOut(BaseModel):
    id: uuid.UUID
    title: str
    owner_id: uuid.UUID
    status: DocumentStatus
    current_version_id: uuid.UUID | None
    created_at: datetime
    is_owner: bool


class DocumentListOut(BaseModel):
    items: list[DocumentOut]
    next_cursor: str | None


class AnalysisOut(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    status: AnalysisStatus
    created_at: datetime


class ExportOut(BaseModel):
    id: uuid.UUID
    analysis_id: uuid.UUID
    format: ExportFormat
    status: ExportStatus
    created_at: datetime


def _document_out(doc: Document, auth: AuthContext) -> DocumentOut:
    return DocumentOut(
        id=doc.id,
        title=doc.title,
        owner_id=doc.owner_id,
        status=doc.status,
        current_version_id=doc.current_version_id,
        created_at=doc.created_at,
        is_owner=doc.owner_id == auth.user.id,
    )


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
    return DocumentListOut(items=[_document_out(d, auth) for d in rows], next_cursor=next_cursor)


@router.get("/documents/{document_id}")
async def get_document(document_id: uuid.UUID, auth: Auth, db: Db) -> DocumentOut:
    document = await get_document_for(db, auth.user, document_id, DocumentAction.view)
    await _audit_superadmin_access(db, auth, document, "document")
    return _document_out(document, auth)


@router.get("/analyses/{analysis_id}")
async def get_analysis(analysis_id: uuid.UUID, auth: Auth, db: Db) -> AnalysisOut:
    analysis = await get_analysis_for(db, auth.user, analysis_id)
    document = await get_document_for(db, auth.user, analysis.document_id, DocumentAction.view)
    await _audit_superadmin_access(db, auth, document, "analysis")
    return AnalysisOut(
        id=analysis.id,
        document_id=analysis.document_id,
        version_id=analysis.version_id,
        status=analysis.status,
        created_at=analysis.created_at,
    )


@router.get("/exports/{export_id}")
async def get_export(export_id: uuid.UUID, auth: Auth, db: Db) -> ExportOut:
    export = await get_export_for(db, auth.user, export_id)
    analysis = await get_analysis_for(db, auth.user, export.analysis_id)
    document = await get_document_for(db, auth.user, analysis.document_id, DocumentAction.view)
    await _audit_superadmin_access(db, auth, document, "export")
    return ExportOut(
        id=export.id,
        analysis_id=export.analysis_id,
        format=export.format,
        status=export.status,
        created_at=export.created_at,
    )
