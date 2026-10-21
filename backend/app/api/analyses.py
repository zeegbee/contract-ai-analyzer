"""API анализов: оценка, запуск, список, карточка с отчётом, риски с доказательствами, отмена, повтор.

Анализ загружается только через `scoped.get_analysis_for` (условие доступа к документу в SQL):
чужой и несуществующий анализ неразличимы — 404. Нехватка роли (Viewer запускает анализ) — 403.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AppSettings, Auth, AuthContext, Db
from app.errors import AppError, PermissionDenied
from app.models.analyses import Analysis
from app.models.documents import Document
from app.models.enums import AnalysisStatus, RiskKind, RiskSeverity, ValidationStatus
from app.schemas.analyses import (
    AnalysisCreateIn,
    AnalysisDetailOut,
    AnalysisListOut,
    AnalysisRetryIn,
    EstimateOut,
    RiskListOut,
)
from app.security.policies import can_upload_documents
from app.security.scoped import (
    DocumentAction,
    get_analysis_for,
    get_document_for,
    is_superadmin_override,
    listed_documents_query,
)
from app.services import analyses as svc
from app.services.audit import AuditAction, record
from app.services.users import decode_cursor, encode_cursor

router = APIRouter(tags=["analyses"])


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


async def _detail(db: AsyncSession, auth: AuthContext, analysis: Analysis) -> AnalysisDetailOut:
    can_analyze = await svc.can_analyze_document(db, auth.user, analysis.document_id)
    title = (await db.execute(select(Document.title).where(Document.id == analysis.document_id))).scalar_one()
    return svc.detail_view(analysis, can_analyze=can_analyze, document_title=title)


def _require_contributor(auth: AuthContext) -> None:
    if not can_upload_documents(auth.user):
        raise PermissionDenied()


@router.post("/analyses/estimate")
async def estimate_analysis(body: AnalysisCreateIn, auth: Auth, db: Db, settings: AppSettings) -> EstimateOut:
    """Оценка объёма и (если цена известна) стоимости — без создания анализа и без передачи данных."""
    plan = await svc.plan_analysis(db, settings, auth.user, body, for_estimate=True)
    return plan.estimate


@router.post("/analyses", status_code=201)
async def create_analysis(
    body: AnalysisCreateIn,
    request: Request,
    response: Response,
    auth: Auth,
    db: Db,
    settings: AppSettings,
    idempotency_key: Annotated[str | None, Header(max_length=128)] = None,
) -> AnalysisDetailOut:
    _require_contributor(auth)
    if idempotency_key is not None and not svc.IDEMPOTENCY_KEY_RE.match(idempotency_key):
        raise AppError("Некорректный Idempotency-Key", code="invalid_idempotency_key")
    analysis, replayed = await svc.create_analysis(
        db,
        settings,
        auth.user,
        ip=auth.ip,
        body=body,
        idempotency_key=idempotency_key,
        box=request.app.state.secret_box,
    )
    if replayed:
        response.status_code = 200
        response.headers["Idempotent-Replayed"] = "true"
    document = await get_document_for(db, auth.user, analysis.document_id, DocumentAction.view)
    await _audit_superadmin_access(db, auth, document, "analysis_start")
    await db.refresh(analysis)
    return await _detail(db, auth, analysis)


@router.get("/analyses")
async def list_analyses(
    auth: Auth,
    db: Db,
    document_id: uuid.UUID | None = None,
    status: AnalysisStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> AnalysisListOut:
    """Анализы доступных документов (свои и явно доступные; Superadmin — только по ID документа)."""
    stmt = select(Analysis, Document.title).join(Document, Document.id == Analysis.document_id)
    if document_id is not None:
        document = await get_document_for(db, auth.user, document_id, DocumentAction.view)
        await _audit_superadmin_access(db, auth, document, "analysis_list")
        stmt = stmt.where(Analysis.document_id == document.id, svc.list_filter(auth.user))
    else:
        visible = listed_documents_query(auth.user).with_only_columns(Document.id)
        stmt = stmt.where(Analysis.document_id.in_(visible.scalar_subquery()))
    if status is not None:
        stmt = stmt.where(Analysis.status == status)
    if cursor:
        created, analysis_id = decode_cursor(cursor)
        stmt = stmt.where(svc.cursor_clause(created, analysis_id))
    stmt = stmt.order_by(Analysis.created_at.desc(), Analysis.id.desc()).limit(limit + 1)
    rows = [(row[0], row[1]) for row in (await db.execute(stmt)).all()]
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor(rows[-1][0].created_at, rows[-1][0].id)
    items = []
    analyzable: dict[uuid.UUID, bool] = {}
    for analysis, title in rows:
        if analysis.document_id not in analyzable:
            analyzable[analysis.document_id] = await svc.can_analyze_document(
                db, auth.user, analysis.document_id
            )
        items.append(
            svc.summary_view(analysis, can_analyze=analyzable[analysis.document_id], document_title=title)
        )
    return AnalysisListOut(items=items, next_cursor=next_cursor)


@router.get("/analyses/{analysis_id}")
async def get_analysis(analysis_id: uuid.UUID, auth: Auth, db: Db) -> AnalysisDetailOut:
    analysis = await get_analysis_for(db, auth.user, analysis_id)
    document = await get_document_for(db, auth.user, analysis.document_id, DocumentAction.view)
    await _audit_superadmin_access(db, auth, document, "analysis")
    return await _detail(db, auth, analysis)


@router.get("/analyses/{analysis_id}/risks")
async def list_risks(
    analysis_id: uuid.UUID,
    auth: Auth,
    db: Db,
    validation_status: ValidationStatus | None = None,
    severity: RiskSeverity | None = None,
    kind: RiskKind | None = None,
    category: Annotated[str | None, Query(max_length=64)] = None,
    include_rejected: bool = False,
) -> RiskListOut:
    """Риски с доказательствами. Отклонённые (`rejected`) по умолчанию скрыты и не являются фактом."""
    analysis = await get_analysis_for(db, auth.user, analysis_id)
    document = await get_document_for(db, auth.user, analysis.document_id, DocumentAction.view)
    await _audit_superadmin_access(db, auth, document, "analysis_risks")
    items, total, hidden = await svc.risk_views(
        db,
        analysis,
        include_rejected=include_rejected,
        filters={
            "validation_status": validation_status,
            "severity": severity,
            "kind": kind,
            "category": category,
        },
    )
    return RiskListOut(items=items, total=total, hidden_rejected=hidden)


@router.post("/analyses/{analysis_id}/cancel")
async def cancel_analysis(analysis_id: uuid.UUID, auth: Auth, db: Db) -> AnalysisDetailOut:
    _require_contributor(auth)
    analysis = await get_analysis_for(db, auth.user, analysis_id, DocumentAction.analyze)
    analysis = await svc.cancel_analysis(db, auth.user, auth.ip, analysis.id)
    return await _detail(db, auth, analysis)


@router.post("/analyses/{analysis_id}/retry")
async def retry_analysis(
    analysis_id: uuid.UUID, auth: Auth, db: Db, settings: AppSettings, body: AnalysisRetryIn | None = None
) -> AnalysisDetailOut:
    _require_contributor(auth)
    analysis = await get_analysis_for(db, auth.user, analysis_id, DocumentAction.analyze)
    analysis = await svc.retry_analysis(
        db,
        settings,
        auth.user,
        ip=auth.ip,
        analysis_id=analysis.id,
        cloud_consent=bool(body and body.cloud_consent),
    )
    return await _detail(db, auth, analysis)
