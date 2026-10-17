"""DTO API анализов. Внутренние результаты этапов (`stage_results`, ответы модели по чанкам) наружу не
отдаются — только проверенные сервером риски, доказательства, покрытие и сводка."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.models.enums import (
    AffectedParty,
    AnalysisDepth,
    AnalysisParty,
    AnalysisStatus,
    MatchStatus,
    RelevanceStatus,
    RiskCategory,
    RiskKind,
    RiskSeverity,
    ValidationStatus,
)

PartyName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class AnalysisCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: uuid.UUID
    # Не указана — текущая версия документа.
    version_id: uuid.UUID | None = None
    party: AnalysisParty
    party_name: PartyName | None = None
    categories: Annotated[list[RiskCategory], Field(min_length=1, max_length=len(RiskCategory))]
    depth: AnalysisDepth = AnalysisDepth.standard
    model_id: uuid.UUID
    # Не указана — смысловую проверку выполняет та же модель.
    verifier_model_id: uuid.UUID | None = None
    # Подтверждение передачи текста внешнему провайдеру — обязательно для облачной модели.
    cloud_consent: bool = False

    @model_validator(mode="after")
    def _check(self) -> AnalysisCreateIn:
        if self.party is AnalysisParty.named and not self.party_name:
            raise ValueError("party_name обязателен для party=named")
        if self.party is not AnalysisParty.named and self.party_name:
            raise ValueError("party_name допускается только для party=named")
        if len(set(self.categories)) != len(self.categories):
            raise ValueError("Категории не должны повторяться")
        return self


class AnalysisRetryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cloud_consent: bool = False


class EstimateOut(BaseModel):
    chunks: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_tokens: int
    token_limit: int
    within_limit: bool
    # Только если цена модели известна (из API провайдера или введена администратором); иначе null.
    estimated_cost: str | None
    cost_currency: str | None
    pricing_source: str | None
    cloud_transfer: bool
    note: str = (
        "Оценка по длине текста (≈3 символа на токен) с верхней границей ответа модели; фактический "
        "расход зависит от модели и ответа."
    )


class ProgressOut(BaseModel):
    stage: str | None = None
    chunks_total: int | None = None
    chunks_done: int | None = None
    risks: int | None = None
    retry_scheduled: str | None = None


class AnalysisSummaryOut(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    created_by_id: uuid.UUID | None
    status: AnalysisStatus
    party: AnalysisParty
    party_name: str | None
    categories: list[str]
    depth: AnalysisDepth
    model_display_name: str | None
    cloud_transfer: bool
    progress: ProgressOut
    error_code: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    can_cancel: bool
    can_retry: bool


class AnalysisDetailOut(AnalysisSummaryOut):
    config_snapshot: dict[str, Any]
    cloud_consent_at: datetime | None
    summary: dict[str, Any] | None
    coverage: dict[str, Any] | None
    usage: dict[str, Any] | None
    estimate: EstimateOut | None = None


class AnalysisListOut(BaseModel):
    items: list[AnalysisSummaryOut]
    next_cursor: str | None


class EvidenceOut(BaseModel):
    id: uuid.UUID
    claimed_fragment_key: str
    fragment_id: uuid.UUID | None
    fragment_key: str | None
    span_fragment_keys: list[str]
    quote: str
    match_status: MatchStatus
    match_char_start: int | None
    match_char_end: int | None
    # Страница и пункт — с сервера, из метаданных версии (не из ответа модели).
    page_start: int | None
    page_end: int | None
    page_source: str | None
    pages_approximate: bool
    clause_ref: str | None
    relevance_status: RelevanceStatus
    relevance_rationale: str | None
    relevance_checked_by: str | None
    match_notes: list[str]
    attempt: int
    # Доказательство заменено повторным запросом цитаты (в итоговом статусе не учитывается).
    superseded: bool


class RiskOut(BaseModel):
    id: uuid.UUID
    risk_key: str
    kind: RiskKind
    title: str
    category: str
    severity: RiskSeverity
    description: str
    reason: str
    consequences: str
    affected_party: AffectedParty
    recommendation: str
    # Проект для согласования, а не гарантированно корректный юридический текст.
    suggested_revision: str | None
    model_confidence: float | None
    validation_status: ValidationStatus
    validation_notes: dict[str, Any]
    search_scope: dict[str, Any] | None
    dedup_group: str | None
    evidence: list[EvidenceOut]


class RiskListOut(BaseModel):
    items: list[RiskOut]
    total: int
    hidden_rejected: int
