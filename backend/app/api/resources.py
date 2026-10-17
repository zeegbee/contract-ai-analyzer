"""Экспорт отчётов анализа: создание, список, статус, скачивание.

Экспорт загружается через `scoped.get_export_for` (анализ → документ, условие доступа в SQL):
чужой и несуществующий экспорт неразличимы — 404. Скачивание — `attachment` + `nosniff`, аудит
`export.downloaded`; обращение Superadmin к чужому документу — `document.accessed_by_superadmin`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AppSettings, Auth, AuthContext, Db
from app.exports.render import MEDIA_TYPES
from app.models.documents import Document
from app.models.enums import ExportFormat, ExportStatus
from app.models.system import Export
from app.security.scoped import (
    DocumentAction,
    get_analysis_for,
    get_document_for,
    get_export_for,
    is_superadmin_override,
)
from app.services import exports as svc
from app.services.audit import AuditAction, record
from app.storage import LocalStorage

router = APIRouter(tags=["exports"])


class ExportCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: ExportFormat


class ExportOut(BaseModel):
    id: uuid.UUID
    analysis_id: uuid.UUID
    format: ExportFormat
    status: ExportStatus
    created_at: datetime
    size_bytes: int | None
    expires_at: datetime | None
    error_code: str | None
    download_url: str | None


class ExportListOut(BaseModel):
    items: list[ExportOut]


def _out(export: Export) -> ExportOut:
    ready = export.status is ExportStatus.ready
    return ExportOut(
        id=export.id,
        analysis_id=export.analysis_id,
        format=export.format,
        status=export.status,
        created_at=export.created_at,
        size_bytes=export.size_bytes,
        expires_at=export.expires_at,
        error_code=export.error_code,
        download_url=f"/api/exports/{export.id}/download" if ready else None,
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


@router.post("/analyses/{analysis_id}/exports", status_code=202)
async def create_export(
    analysis_id: uuid.UUID, body: ExportCreateIn, auth: Auth, db: Db, settings: AppSettings
) -> ExportOut:
    analysis = await get_analysis_for(db, auth.user, analysis_id)
    document = await get_document_for(db, auth.user, analysis.document_id, DocumentAction.view)
    await _audit_superadmin_access(db, auth, document, "export_create")
    export = await svc.create_export(db, settings, auth.user, ip=auth.ip, analysis=analysis, fmt=body.format)
    return _out(export)


@router.get("/analyses/{analysis_id}/exports")
async def list_exports(analysis_id: uuid.UUID, auth: Auth, db: Db) -> ExportListOut:
    analysis = await get_analysis_for(db, auth.user, analysis_id)
    rows = (
        await db.execute(
            select(Export)
            .where(Export.analysis_id == analysis.id)
            .order_by(Export.created_at.desc(), Export.id.desc())
            .limit(50)
        )
    ).scalars()
    return ExportListOut(items=[_out(e) for e in rows])


@router.get("/exports/{export_id}")
async def get_export(export_id: uuid.UUID, auth: Auth, db: Db) -> ExportOut:
    export = await get_export_for(db, auth.user, export_id)
    analysis = await get_analysis_for(db, auth.user, export.analysis_id)
    document = await get_document_for(db, auth.user, analysis.document_id, DocumentAction.view)
    await _audit_superadmin_access(db, auth, document, "export")
    return _out(export)


@router.get("/exports/{export_id}/download")
async def download_export(export_id: uuid.UUID, request: Request, auth: Auth, db: Db) -> FileResponse:
    export = await get_export_for(db, auth.user, export_id)
    key = svc.ensure_downloadable(export)
    analysis = await get_analysis_for(db, auth.user, export.analysis_id)
    document = await get_document_for(db, auth.user, analysis.document_id, DocumentAction.view)
    await _audit_superadmin_access(db, auth, document, "export_download")
    record(
        db,
        AuditAction.export_downloaded,
        actor_id=auth.user.id,
        target_type="export",
        target_id=export.id,
        ip=auth.ip,
        details={"analysis_id": str(analysis.id), "format": export.format.value},
    )
    await db.commit()
    storage: LocalStorage = request.app.state.storage
    return FileResponse(
        storage.local_path(key),
        media_type=MEDIA_TYPES[export.format.value],
        filename=svc.download_name(export),
        content_disposition_type="attachment",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
    )
