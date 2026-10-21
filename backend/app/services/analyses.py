"""Анализы: запуск (проверки прав, модели, облака, лимитов, идемпотентность), отмена, повтор,
восстановление «потерянных» анализов и представления для API (ARCHITECTURE §9.1, §13).

Права на документ проверяются scoped-запросами (`app.security.scoped`) — чужое → 404.
Модель — `catalog.resolve_model_for_use` (то же правило, что `/models/available`).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import String, and_, cast, func, literal, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis import (
    CHUNKER_VERSION,
    INJECTION_DETECTOR_VERSION,
    NORMALIZER_VERSION,
    PIPELINE_VERSION,
    SCHEMA_VERSION,
)
from app.analysis.chunking import (
    CHARS_PER_TOKEN,
    OVERLAP_SHARE,
    PROMPT_RESERVE_TOKENS,
    ContextTooSmall,
    FragmentSize,
    PlannedChunk,
    estimate_tokens,
    plan_chunks,
    text_budget_tokens,
)
from app.analysis.errors import safe_message
from app.analysis.prompts import PROMPT_VERSIONS
from app.config import Settings
from app.errors import AppError, ConflictError, PermissionDenied
from app.jobs import queue
from app.models.analyses import Analysis, AnalysisChunk, AnalysisRisk, RiskEvidence
from app.models.catalog import ModelConfig, ProviderConfig
from app.models.documents import Document, DocumentFragment, DocumentVersion
from app.models.enums import (
    AnalysisDepth,
    AnalysisStatus,
    ChunkStatus,
    ExtractionStatus,
    JobStatus,
    PageSource,
    RedactionMode,
    RiskCategory,
)
from app.models.redaction import AnalysisRedaction
from app.models.system import Job
from app.models.users import ConsentRecord, User
from app.schemas.analyses import (
    AnalysisCreateIn,
    AnalysisDetailOut,
    AnalysisSummaryOut,
    EstimateOut,
    EvidenceOut,
    ProgressOut,
    RedactionSummaryOut,
    RiskOut,
)
from app.schemas.catalog import ModelPurpose
from app.security import policies
from app.security.scoped import (
    DocumentAction,
    document_access_clause,
    get_document_for,
    get_version_for,
)
from app.security.secretbox import SecretBox
from app.services import catalog, redaction
from app.services.audit import AuditAction, record

RUN_ANALYSIS_JOB = "run_analysis"
RUN_ANALYSIS_MAX_ATTEMPTS = 4
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
ACTIVE = (AnalysisStatus.queued, AnalysisStatus.running, AnalysisStatus.verifying, AnalysisStatus.cancelling)
CANCELLABLE = (AnalysisStatus.queued, AnalysisStatus.running, AnalysisStatus.verifying)
RETRYABLE = (AnalysisStatus.failed, AnalysisStatus.cancelled)
# Оценочные объёмы вспомогательных вызовов (структура, верификатор, повтор цитаты).
_STRUCTURE_INPUT_CHARS = 9000
_VERIFIER_CALLS_PER_CHUNK = 3
_VERIFIER_INPUT_TOKENS = 1500
_AUX_OUTPUT_TOKENS = 2048


class AnalysisLimitExceeded(AppError):
    status_code = 429
    code = "too_many_active_analyses"
    message = "Слишком много выполняющихся анализов. Дождитесь завершения или отмените один из них."


def dedup_key(analysis_id: uuid.UUID) -> str:
    return f"{RUN_ANALYSIS_JOB}:{analysis_id}"


def _now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------ планирование и оценка


@dataclass
class Plan:
    document: Document
    version: DocumentVersion
    model: ModelConfig
    provider: ProviderConfig
    verifier: tuple[ModelConfig, ProviderConfig] | None
    chunks: list[PlannedChunk]
    budget_tokens: int
    max_output_tokens: int
    estimate: EstimateOut

    @property
    def cloud(self) -> bool:
        return self.provider.is_cloud or bool(self.verifier and self.verifier[1].is_cloud)


def _cost(
    pricing: dict[str, Any] | None, tokens_in: int, tokens_out: int
) -> tuple[str | None, str | None, str | None]:
    if not pricing:
        return None, None, None
    try:
        prompt = Decimal(str(pricing["prompt_per_mtok"]))
        completion = Decimal(str(pricing["completion_per_mtok"]))
    except (KeyError, ArithmeticError, ValueError):
        return None, None, None
    cost = (prompt * tokens_in + completion * tokens_out) / Decimal(1_000_000)
    return str(cost.quantize(Decimal("0.0001"))), pricing.get("currency"), pricing.get("source")


def build_estimate(
    settings: Settings,
    *,
    chunks: list[PlannedChunk],
    budget_tokens: int,
    max_output_tokens: int,
    depth: AnalysisDepth,
    categories: list[RiskCategory],
    pricing: dict[str, Any] | None,
    cloud: bool,
) -> EstimateOut:
    """Верхняя оценка токенов: все вызовы с полным лимитом ответа (фактический расход обычно меньше)."""
    tokens_in = sum(c.est_tokens + PROMPT_RESERVE_TOKENS for c in chunks)
    tokens_out = len(chunks) * max_output_tokens
    if depth is not AnalysisDepth.quick:
        tokens_in += estimate_tokens(_STRUCTURE_INPUT_CHARS) + PROMPT_RESERVE_TOKENS
        tokens_out += _AUX_OUTPUT_TOKENS
    if RiskCategory.contradiction in categories or RiskCategory.missing_clause in categories:
        tokens_in += budget_tokens + PROMPT_RESERVE_TOKENS
        tokens_out += max_output_tokens
    verifier_calls = len(chunks) * _VERIFIER_CALLS_PER_CHUNK
    tokens_in += verifier_calls * _VERIFIER_INPUT_TOKENS
    tokens_out += verifier_calls * _AUX_OUTPUT_TOKENS
    total = tokens_in + tokens_out
    cost, currency, source = _cost(pricing, tokens_in, tokens_out)
    return EstimateOut(
        chunks=len(chunks),
        estimated_input_tokens=tokens_in,
        estimated_output_tokens=tokens_out,
        estimated_tokens=total,
        token_limit=settings.analysis_max_tokens,
        within_limit=total <= settings.analysis_max_tokens,
        estimated_cost=cost,
        cost_currency=currency,
        pricing_source=source,
        cloud_transfer=cloud,
    )


async def _require_cloud_notice(db: AsyncSession, user: User, provider: ProviderConfig) -> None:
    """Для облачного провайдера пользователь должен был принять текущую версию предупреждения."""
    notice = await catalog.cloud_notice(db)
    accepted = (
        await db.execute(
            select(ConsentRecord.id).where(
                ConsentRecord.user_id == user.id,
                ConsentRecord.provider_config_id == provider.id,
                ConsentRecord.notice_version == notice.version,
            )
        )
    ).first()
    if accepted is None:
        raise ConflictError(
            "Ознакомьтесь с предупреждением о передаче текста внешнему провайдеру и подтвердите его",
            code="cloud_notice_not_accepted",
            details={"notice_version": notice.version, "provider_id": str(provider.id)},
        )


async def plan_analysis(
    db: AsyncSession, settings: Settings, user: User, body: AnalysisCreateIn, *, for_estimate: bool = False
) -> Plan:
    if not policies.can_upload_documents(user):
        raise PermissionDenied()
    document = await get_document_for(db, user, body.document_id, DocumentAction.analyze)
    version_id = body.version_id or document.current_version_id
    if version_id is None:
        raise ConflictError("У документа нет версии для анализа", code="version_not_ready")
    _doc, version = await get_version_for(db, user, document.id, version_id, DocumentAction.analyze)
    if version.extraction_status is not ExtractionStatus.ready:
        raise ConflictError(
            "Текст версии ещё не извлечён или извлечение не удалось", code="version_not_ready"
        )

    # Для оценки согласие не нужно (данные не передаются); при запуске — обязательно.
    consent = True if for_estimate else body.cloud_consent
    model, provider = await catalog.resolve_model_for_use(
        db, user, body.model_id, purpose=ModelPurpose.analysis, cloud_consent=consent
    )
    verifier: tuple[ModelConfig, ProviderConfig] | None = None
    if body.verifier_model_id is not None and body.verifier_model_id != model.id:
        verifier = await catalog.resolve_model_for_use(
            db, user, body.verifier_model_id, purpose=ModelPurpose.verifier, cloud_consent=consent
        )
    if not for_estimate:
        providers = [provider] + ([verifier[1]] if verifier and verifier[1].id != provider.id else [])
        for p in providers:
            if p.is_cloud:
                await _require_cloud_notice(db, user, p)

    sizes = await fragment_sizes(db, version.id)
    if not sizes:
        raise ConflictError(safe_message("version_has_no_text"), code="version_has_no_text")
    return _plan_chunks(
        settings,
        body,
        document=document,
        version=version,
        model=model,
        provider=provider,
        verifier=verifier,
        sizes=list(sizes.values()),
    )


async def fragment_sizes(db: AsyncSession, version_id: uuid.UUID) -> dict[str, FragmentSize]:
    """Ключ фрагмента → (seq, длина текста) в порядке текста."""
    rows = (
        await db.execute(
            select(
                DocumentFragment.fragment_key, DocumentFragment.seq, func.char_length(DocumentFragment.text)
            )
            .where(DocumentFragment.version_id == version_id)
            .order_by(DocumentFragment.seq)
        )
    ).all()
    return {str(key): FragmentSize(seq, chars) for key, seq, chars in rows}


def _plan_chunks(
    settings: Settings,
    body: AnalysisCreateIn,
    *,
    document: Document,
    version: DocumentVersion,
    model: ModelConfig,
    provider: ProviderConfig,
    verifier: tuple[ModelConfig, ProviderConfig] | None,
    sizes: list[FragmentSize],
) -> Plan:
    max_output = model.max_output_tokens or settings.analysis_default_max_output_tokens
    try:
        budget = text_budget_tokens(model.context_window, max_output, body.depth)
    except ContextTooSmall:
        raise AppError(
            "Контекст модели слишком мал для анализа с текущим лимитом ответа",
            code="model_context_too_small",
            status_code=422,
        ) from None
    chunks = plan_chunks(version.id, sizes, budget)
    cloud = provider.is_cloud or bool(verifier and verifier[1].is_cloud)
    estimate = build_estimate(
        settings,
        chunks=chunks,
        budget_tokens=budget,
        max_output_tokens=max_output,
        depth=body.depth,
        categories=body.categories,
        pricing=model.pricing,
        cloud=cloud,
    )
    return Plan(document, version, model, provider, verifier, chunks, budget, max_output, estimate)


async def replan_masked(
    db: AsyncSession, settings: Settings, body: AnalysisCreateIn, plan: Plan, mapping: dict[str, Any]
) -> Plan:
    """Чанки и оценка по длинам замаскированного текста (метки длиннее или короче значений)."""
    sizes = await fragment_sizes(db, plan.version.id)
    masked = redaction.masked_sizes(mapping, {k: s.chars for k, s in sizes.items()})
    return _plan_chunks(
        settings,
        body,
        document=plan.document,
        version=plan.version,
        model=plan.model,
        provider=plan.provider,
        verifier=plan.verifier,
        sizes=[FragmentSize(s.seq, masked[k]) for k, s in sizes.items()],
    )


def build_snapshot(settings: Settings, plan: Plan) -> dict[str, Any]:
    snapshot = catalog.config_snapshot(plan.model, plan.provider)
    if plan.verifier is None:
        snapshot["verifier"] = {"same_as_analysis": True}
    else:
        snapshot["verifier"] = {"same_as_analysis": False, **catalog.config_snapshot(*plan.verifier)}
    snapshot["pipeline"] = {
        "pipeline": PIPELINE_VERSION,
        "schema": SCHEMA_VERSION,
        "prompts": dict(PROMPT_VERSIONS),
        "chunker": CHUNKER_VERSION,
        "normalizer": NORMALIZER_VERSION,
        "injection_detector": INJECTION_DETECTOR_VERSION,
    }
    snapshot["chunking"] = {
        "budget_tokens": plan.budget_tokens,
        "chars_per_token": CHARS_PER_TOKEN,
        "overlap_share": OVERLAP_SHARE,
        "max_output_tokens": plan.max_output_tokens,
        "chunks": len(plan.chunks),
    }
    snapshot["limits"] = {
        "max_analysis_tokens": settings.analysis_max_tokens,
        "quote_retry_limit": settings.analysis_quote_retry_limit,
    }
    snapshot["estimate"] = plan.estimate.model_dump(mode="json")
    snapshot["document"] = {
        "version_no": plan.version.version_no,
        "format": plan.version.detected_format.value,
        "page_source": plan.version.page_source.value if plan.version.page_source else None,
        "extractor_version": plan.version.extractor_version,
    }
    return snapshot


# ------------------------------------------------------------------ запуск


async def find_by_idempotency_key(db: AsyncSession, user_id: uuid.UUID, key: str) -> Analysis | None:
    return (
        await db.execute(
            select(Analysis).where(Analysis.created_by_id == user_id, Analysis.idempotency_key == key)
        )
    ).scalar_one_or_none()


def same_request(analysis: Analysis, body: AnalysisCreateIn) -> bool:
    return (
        analysis.document_id == body.document_id
        and (body.version_id is None or analysis.version_id == body.version_id)
        and analysis.party == body.party
        and (analysis.party_name or None) == (body.party_name or None)
        and sorted(analysis.categories) == sorted(c.value for c in body.categories)
        and analysis.depth == body.depth
        and analysis.model_config_id == body.model_id
    )


async def active_count(db: AsyncSession, user_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(Analysis)
                .where(Analysis.created_by_id == user_id, Analysis.status.in_(ACTIVE))
            )
        ).scalar_one()
    )


async def _apply_redaction(
    db: AsyncSession, settings: Settings, user: User, *, box: SecretBox, body: AnalysisCreateIn, plan: Plan
) -> tuple[Plan, redaction.ConsumedPreview | None]:
    """Облачная модель — только после подтверждённого предпросмотра маскировки (этап 10)."""
    if not plan.cloud:
        if body.redaction_preview_id is not None:
            raise AppError(
                "Маскировка применяется только к облачным моделям",
                code="redaction_not_applicable",
                status_code=422,
            )
        return plan, None
    if body.redaction_preview_id is None:
        raise AppError(
            "Проверьте и подтвердите маскировку перед облачным анализом",
            code="redaction_preview_required",
            status_code=422,
        )
    consumed = await redaction.consume_preview(
        db, user, box, preview_id=body.redaction_preview_id, version=plan.version
    )
    if consumed.mapping is not None:
        plan = await replan_masked(db, settings, body, plan, consumed.mapping)
    return plan, consumed


async def create_analysis(
    db: AsyncSession,
    settings: Settings,
    user: User,
    *,
    ip: str | None,
    body: AnalysisCreateIn,
    idempotency_key: str | None,
    box: SecretBox,
) -> tuple[Analysis, bool]:
    """Создаёт анализ, чанки и задачу одной транзакцией. Возвращает (анализ, повтор по ключу)."""
    if idempotency_key:
        existing = await find_by_idempotency_key(db, user.id, idempotency_key)
        if existing is not None:
            if not same_request(existing, body):
                raise ConflictError(
                    "Idempotency-Key уже использован для другого запроса", code="idempotency_key_reused"
                )
            # Права проверяются и для повтора: доступ к документу мог быть отозван.
            await get_document_for(db, user, existing.document_id, DocumentAction.view)
            return existing, True

    plan = await plan_analysis(db, settings, user, body)
    plan, consumed = await _apply_redaction(db, settings, user, box=box, body=body, plan=plan)
    if not plan.estimate.within_limit:
        raise AppError(
            "Оценка объёма анализа превышает лимит токенов. Выберите меньшую глубину или другую модель.",
            code="analysis_budget_exceeded",
            status_code=422,
            details={"estimated_tokens": plan.estimate.estimated_tokens, "limit": plan.estimate.token_limit},
        )
    # Сериализация запусков одного пользователя: лимит активных анализов не обходится параллельными запросами.
    await db.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"analysis-start:{user.id}"))))
    if await active_count(db, user.id) >= settings.analysis_max_active_per_user:
        raise AnalysisLimitExceeded(details={"limit": settings.analysis_max_active_per_user})

    now = _now()
    snapshot = build_snapshot(settings, plan)
    snapshot["redaction"] = (
        {
            "mode": consumed.mode.value,
            "redaction_version": consumed.preview.redaction_version,
            "settings_revision": consumed.settings_revision,
            "counts": consumed.counts,
            "unmasked_confirmed_at": consumed.preview.unmasked_confirmed_at.isoformat()
            if consumed.preview.unmasked_confirmed_at
            else None,
        }
        if consumed is not None
        else None
    )
    analysis = Analysis(
        id=uuid.uuid4(),
        document_id=plan.document.id,
        version_id=plan.version.id,
        created_by_id=user.id,
        status=AnalysisStatus.queued,
        party=body.party,
        party_name=body.party_name,
        categories=[c.value for c in body.categories],
        depth=body.depth,
        model_config_id=plan.model.id,
        config_snapshot=snapshot,
        cloud_transfer=plan.cloud,
        cloud_consent_at=now if plan.cloud else None,
        idempotency_key=idempotency_key,
        progress={"stage": "queued", "chunks_total": len(plan.chunks), "chunks_done": 0},
    )
    db.add(analysis)
    await db.flush()
    if consumed is not None:
        redaction.attach(db, box, analysis.id, consumed)
    for chunk in plan.chunks:
        db.add(
            AnalysisChunk(
                analysis_id=analysis.id,
                chunk_index=chunk.index,
                chunk_key=chunk.key,
                fragment_seq_start=chunk.seq_start,
                fragment_seq_end=chunk.seq_end,
                overlap_from_seq=chunk.overlap_from_seq,
                status=ChunkStatus.pending,
            )
        )
    await queue.enqueue(
        db,
        RUN_ANALYSIS_JOB,
        {"analysis_id": str(analysis.id)},
        priority=100,
        max_attempts=RUN_ANALYSIS_MAX_ATTEMPTS,
        dedup_key=dedup_key(analysis.id),
    )
    record(
        db,
        AuditAction.analysis_started,
        actor_id=user.id,
        target_type="analysis",
        target_id=analysis.id,
        ip=ip,
        details={
            "document_id": str(plan.document.id),
            "version_id": str(plan.version.id),
            "model": str(plan.model.id),
            "verifier_model": str(plan.verifier[0].id) if plan.verifier else None,
            "cloud": plan.cloud,
            "party": body.party.value,
            "depth": body.depth.value,
            "categories": len(body.categories),
            "chunks": len(plan.chunks),
            "estimated_tokens": plan.estimate.estimated_tokens,
            "redaction": consumed.mode.value if consumed is not None else "not_applicable",
            "redaction_values": consumed.counts.get("values", 0) if consumed is not None else 0,
        },
    )
    if consumed is not None and consumed.mode is RedactionMode.unmasked_by_user:
        record(
            db,
            AuditAction.redaction_sent_unmasked,
            actor_id=user.id,
            target_type="analysis",
            target_id=analysis.id,
            ip=ip,
            details={
                "document_id": str(plan.document.id),
                "provider": str(plan.provider.id),
                "categories": consumed.counts.get("categories", {}),
                "values": consumed.counts.get("values", 0),
            },
        )
    try:
        await db.commit()
    except IntegrityError:
        # Параллельный запрос с тем же Idempotency-Key успел первым.
        await db.rollback()
        if idempotency_key:
            existing = await find_by_idempotency_key(db, user.id, idempotency_key)
            if existing is not None and same_request(existing, body):
                return existing, True
        raise ConflictError("Idempotency-Key уже использован", code="idempotency_key_reused") from None
    return analysis, False


# ------------------------------------------------------------------ отмена и повтор


async def _locked(db: AsyncSession, analysis_id: uuid.UUID) -> Analysis:
    return (
        await db.execute(select(Analysis).where(Analysis.id == analysis_id).with_for_update())
    ).scalar_one()


async def cancel_analysis(db: AsyncSession, user: User, ip: str | None, analysis_id: uuid.UUID) -> Analysis:
    analysis = await _locked(db, analysis_id)
    if analysis.status not in CANCELLABLE:
        raise ConflictError("Анализ уже завершён или отменяется", code="analysis_not_cancellable")
    previous = analysis.status
    analysis.cancel_requested_at = _now()
    # Задача ещё не взята worker'ом — отменяем сразу; иначе worker остановится на ближайшей проверке.
    cancelled_jobs = (
        await db.execute(
            update(Job)
            .where(Job.dedup_key == dedup_key(analysis.id), Job.status == JobStatus.queued)
            .values(status=JobStatus.cancelled, finished_at=func.now())
            .returning(Job.id)
        )
    ).all()
    running = (
        await db.execute(
            select(Job.id).where(Job.dedup_key == dedup_key(analysis.id), Job.status == JobStatus.running)
        )
    ).first()
    if running is None:
        analysis.status = AnalysisStatus.cancelled
        analysis.finished_at = _now()
        progress = dict(analysis.progress or {})
        progress["stage"] = "cancelled"
        analysis.progress = progress
    else:
        analysis.status = AnalysisStatus.cancelling
    record(
        db,
        AuditAction.analysis_cancelled,
        actor_id=user.id,
        target_type="analysis",
        target_id=analysis.id,
        ip=ip,
        details={"previous_status": previous.value, "queued_jobs_cancelled": len(cancelled_jobs)},
    )
    await db.commit()
    await db.refresh(analysis)
    return analysis


async def retry_analysis(
    db: AsyncSession,
    settings: Settings,
    user: User,
    *,
    ip: str | None,
    analysis_id: uuid.UUID,
    cloud_consent: bool,
) -> Analysis:
    analysis = await _locked(db, analysis_id)
    if analysis.status not in RETRYABLE:
        raise ConflictError(
            "Повторить можно только неудачный или отменённый анализ", code="analysis_not_retryable"
        )
    snapshot = analysis.config_snapshot
    # Модель — из snapshot, но права и доступность проверяются по текущему состоянию.
    model_id = uuid.UUID(snapshot["model"]["id"])
    await catalog.resolve_model_for_use(db, user, model_id, cloud_consent=cloud_consent)
    verifier = snapshot.get("verifier") or {}
    if not verifier.get("same_as_analysis", True):
        await catalog.resolve_model_for_use(
            db,
            user,
            uuid.UUID(verifier["model"]["id"]),
            purpose=ModelPurpose.verifier,
            cloud_consent=cloud_consent,
        )
    if analysis.cloud_transfer:
        row = await db.get(AnalysisRedaction, analysis.id)
        if row is None:
            # Облачный анализ до этапа 10 (без маскировки) не повторяется — только новый запуск.
            raise ConflictError(
                "Анализ запущен без проверки маскировки. Запустите новый анализ.",
                code="redaction_required_for_retry",
            )
        allowed = (await redaction.get_state(db)).settings.allow_unmasked
        if row.mode is RedactionMode.unmasked_by_user and not allowed:
            raise PermissionDenied(
                "Отправка без маскировки запрещена настройками", code="redaction_unmasked_not_allowed"
            )
        for provider_id in {snapshot["provider"]["id"], verifier.get("provider", {}).get("id")} - {None}:
            provider = await db.get(ProviderConfig, uuid.UUID(str(provider_id)))
            if provider is not None and provider.is_cloud:
                await _require_cloud_notice(db, user, provider)
    await db.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"analysis-start:{user.id}"))))
    if await active_count(db, user.id) >= settings.analysis_max_active_per_user:
        raise AnalysisLimitExceeded(details={"limit": settings.analysis_max_active_per_user})

    materialized = bool((analysis.stage_results or {}).get("materialized"))
    if not materialized:
        # Неудачные и пропущенные чанки — заново; готовые не пересчитываются.
        await db.execute(
            update(AnalysisChunk)
            .where(
                AnalysisChunk.analysis_id == analysis.id,
                AnalysisChunk.status.in_([ChunkStatus.failed, ChunkStatus.skipped, ChunkStatus.running]),
            )
            .values(status=ChunkStatus.pending, error_code=None)
        )
    previous = analysis.status
    analysis.status = AnalysisStatus.verifying if materialized else AnalysisStatus.queued
    analysis.cancel_requested_at = None
    analysis.finished_at = None
    analysis.error_code = None
    analysis.error_message_safe = None
    progress = dict(analysis.progress or {})
    progress.update(stage="queued", retry_scheduled=None)
    analysis.progress = progress
    if analysis.cloud_transfer:
        analysis.cloud_consent_at = _now()
    job = await queue.enqueue(
        db,
        RUN_ANALYSIS_JOB,
        {"analysis_id": str(analysis.id)},
        max_attempts=RUN_ANALYSIS_MAX_ATTEMPTS,
        dedup_key=dedup_key(analysis.id),
    )
    if job is None:
        raise ConflictError("Анализ уже выполняется", code="analysis_already_running")
    record(
        db,
        AuditAction.analysis_retried,
        actor_id=user.id,
        target_type="analysis",
        target_id=analysis.id,
        ip=ip,
        details={"previous_status": previous.value, "cloud": analysis.cloud_transfer},
    )
    await db.commit()
    await db.refresh(analysis)
    return analysis


async def reconcile_orphans(db: AsyncSession, *, grace_seconds: int = 120) -> int:
    """Анализы в активном статусе без активной задачи (задача `dead`/`failed`/отменена) → failed/cancelled.

    Вызывается worker'ом после reaper'а: так анализ не «зависает», если worker упал на всех попытках.
    """
    active_job = (
        select(Job.id)
        .where(
            Job.dedup_key == literal(f"{RUN_ANALYSIS_JOB}:") + cast(Analysis.id, String),
            Job.status.in_([JobStatus.queued, JobStatus.running]),
        )
        .exists()
    )
    stale = and_(
        Analysis.status.in_(ACTIVE),
        ~active_job,
        Analysis.updated_at < func.now() - timedelta(seconds=grace_seconds),
    )
    rows = (await db.execute(select(Analysis).where(stale).with_for_update(skip_locked=True))).scalars().all()
    for analysis in rows:
        if analysis.status is AnalysisStatus.cancelling or analysis.cancel_requested_at is not None:
            analysis.status = AnalysisStatus.cancelled
        else:
            analysis.status = AnalysisStatus.failed
            analysis.error_code = "job_lost"
            analysis.error_message_safe = safe_message("job_lost")
        analysis.finished_at = _now()
    await db.execute(
        update(AnalysisChunk)
        .where(
            AnalysisChunk.analysis_id.in_([a.id for a in rows]), AnalysisChunk.status == ChunkStatus.running
        )
        .values(status=ChunkStatus.pending)
    )
    await db.commit()
    return len(rows)


# ------------------------------------------------------------------ представления


def _can_cancel(analysis: Analysis, can_analyze: bool) -> bool:
    return can_analyze and analysis.status in CANCELLABLE


def _can_retry(analysis: Analysis, can_analyze: bool) -> bool:
    return can_analyze and analysis.status in RETRYABLE


def summary_view(analysis: Analysis, *, can_analyze: bool, document_title: str) -> AnalysisSummaryOut:
    progress = analysis.progress or {}
    return AnalysisSummaryOut(
        id=analysis.id,
        document_id=analysis.document_id,
        document_title=document_title,
        version_id=analysis.version_id,
        created_by_id=analysis.created_by_id,
        status=analysis.status,
        party=analysis.party,
        party_name=analysis.party_name,
        categories=list(analysis.categories),
        depth=analysis.depth,
        model_display_name=(analysis.config_snapshot.get("model") or {}).get("display_name"),
        cloud_transfer=analysis.cloud_transfer,
        progress=ProgressOut(
            stage=progress.get("stage"),
            chunks_total=progress.get("chunks_total"),
            chunks_done=progress.get("chunks_done"),
            risks=progress.get("risks"),
            retry_scheduled=progress.get("retry_scheduled"),
        ),
        error_code=analysis.error_code,
        error_message=analysis.error_message_safe,
        created_at=analysis.created_at,
        started_at=analysis.started_at,
        finished_at=analysis.finished_at,
        can_cancel=_can_cancel(analysis, can_analyze),
        can_retry=_can_retry(analysis, can_analyze),
    )


def _redaction_summary(snapshot: dict[str, Any]) -> RedactionSummaryOut | None:
    info = snapshot.get("redaction")
    if not info:
        return None
    counts = info.get("counts") or {}
    return RedactionSummaryOut(
        mode=info["mode"],
        values=int(counts.get("values", 0)),
        occurrences=int(counts.get("occurrences", 0)),
        categories=dict(counts.get("categories", {})),
        unmasked_confirmed_at=info.get("unmasked_confirmed_at"),
    )


def detail_view(analysis: Analysis, *, can_analyze: bool, document_title: str) -> AnalysisDetailOut:
    snapshot = dict(analysis.config_snapshot)
    estimate = snapshot.get("estimate")
    return AnalysisDetailOut(
        **summary_view(analysis, can_analyze=can_analyze, document_title=document_title).model_dump(),
        config_snapshot=snapshot,
        cloud_consent_at=analysis.cloud_consent_at,
        summary=analysis.summary,
        coverage=analysis.coverage,
        usage=analysis.usage,
        estimate=EstimateOut.model_validate(estimate) if estimate else None,
        redaction=_redaction_summary(snapshot),
    )


async def can_analyze_document(db: AsyncSession, user: User, document_id: uuid.UUID) -> bool:
    row = (
        await db.execute(
            select(Document.id).where(
                Document.id == document_id, document_access_clause(user, DocumentAction.analyze)
            )
        )
    ).first()
    return row is not None


async def risk_views(
    db: AsyncSession,
    analysis: Analysis,
    *,
    include_rejected: bool,
    filters: dict[str, Any],
) -> tuple[list[RiskOut], int, int]:
    stmt = select(AnalysisRisk).where(AnalysisRisk.analysis_id == analysis.id)
    for column, value in filters.items():
        if value is not None:
            stmt = stmt.where(getattr(AnalysisRisk, column) == value)
    risks = list((await db.execute(stmt.order_by(AnalysisRisk.risk_key))).scalars())
    total = len(risks)
    hidden = 0
    if not include_rejected and filters.get("validation_status") is None:
        hidden = sum(1 for r in risks if r.validation_status.value == "rejected")
        risks = [r for r in risks if r.validation_status.value != "rejected"]
    ids = [r.id for r in risks]
    evidence = (
        list(
            (
                await db.execute(
                    select(RiskEvidence)
                    .where(RiskEvidence.risk_id.in_(ids))
                    .order_by(RiskEvidence.attempt, RiskEvidence.created_at, RiskEvidence.id)
                )
            ).scalars()
        )
        if ids
        else []
    )
    fragment_ids = {fid for e in evidence for fid in [e.fragment_id, *e.fragment_ids_span] if fid}
    keys = (
        dict(
            (
                await db.execute(
                    select(DocumentFragment.id, DocumentFragment.fragment_key).where(
                        DocumentFragment.id.in_(fragment_ids)
                    )
                )
            ).all()
        )
        if fragment_ids
        else {}
    )
    page_source = analysis.config_snapshot.get("document", {}).get("page_source")
    approximate = page_source in (PageSource.libreoffice_render.value, PageSource.docx_rendered_breaks.value)
    by_risk: dict[uuid.UUID, list[RiskEvidence]] = {}
    for e in evidence:
        by_risk.setdefault(e.risk_id, []).append(e)
    items = []
    for r in risks:
        rows = by_risk.get(r.id, [])
        last = max((e.attempt for e in rows), default=0)
        items.append(
            RiskOut(
                id=r.id,
                risk_key=r.risk_key,
                kind=r.kind,
                title=r.title,
                category=r.category,
                severity=r.severity,
                description=r.description,
                reason=r.reason,
                consequences=r.consequences,
                affected_party=r.affected_party,
                recommendation=r.recommendation,
                suggested_revision=r.suggested_revision,
                model_confidence=r.model_confidence,
                validation_status=r.validation_status,
                validation_notes=dict(r.validation_notes or {}),
                search_scope=r.search_scope,
                dedup_group=r.dedup_group,
                evidence=[
                    EvidenceOut(
                        id=e.id,
                        claimed_fragment_key=e.claimed_fragment_key,
                        fragment_id=e.fragment_id,
                        fragment_key=keys.get(e.fragment_id) if e.fragment_id else None,
                        span_fragment_keys=[keys[f] for f in e.fragment_ids_span if f in keys],
                        quote=e.quote,
                        match_status=e.match_status,
                        match_char_start=e.match_char_start,
                        match_char_end=e.match_char_end,
                        page_start=e.page_start,
                        page_end=e.page_end,
                        page_source=page_source if e.page_start is not None else None,
                        pages_approximate=approximate and e.page_start is not None,
                        clause_ref=e.clause_ref,
                        relevance_status=e.relevance_status,
                        relevance_rationale=e.relevance_rationale,
                        relevance_checked_by=e.relevance_checked_by,
                        match_notes=list((e.verifier_model_snapshot or {}).get("match_notes", [])),
                        attempt=e.attempt,
                        superseded=e.attempt < last,
                    )
                    for e in rows
                ],
            )
        )
    return items, total, hidden


def list_filter(user: User) -> Any:
    return document_access_clause(user, DocumentAction.view)


def cursor_clause(created: datetime, analysis_id: uuid.UUID) -> Any:
    return or_(
        Analysis.created_at < created,
        and_(Analysis.created_at == created, Analysis.id < analysis_id),
    )
