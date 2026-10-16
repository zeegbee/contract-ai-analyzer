"""Чтение метаданных анализов и экспортов с проверкой доступа (документы — `app.api.documents`).

Запуск анализа и экспорт появятся на этапах 06–07; здесь — только чтение, чтобы права доступа
(scoped-запросы, 404 для чужих объектов) действовали с самого начала.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Auth, AuthContext, Db
from app.models.documents import Document
from app.models.enums import AnalysisStatus, ExportFormat, ExportStatus
from app.security.scoped import (
    DocumentAction,
    get_analysis_for,
    get_document_for,
    get_export_for,
    is_superadmin_override,
)
from app.services.audit import AuditAction, record

router = APIRouter(tags=["documents"])


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
