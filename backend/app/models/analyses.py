"""Анализы, чанки, риски и доказательства."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAt, Timestamps, UUIDPk, str_enum, tz_datetime
from app.models.enums import (
    AffectedParty,
    AnalysisDepth,
    AnalysisParty,
    AnalysisStatus,
    ChunkStatus,
    MatchStatus,
    RelevanceStatus,
    RiskKind,
    RiskSeverity,
    ValidationStatus,
)


class Analysis(UUIDPk, Timestamps, Base):
    __tablename__ = "analyses"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[AnalysisStatus] = mapped_column(
        str_enum(AnalysisStatus, "analysis_status"),
        nullable=False,
        server_default=AnalysisStatus.queued.value,
    )
    party: Mapped[AnalysisParty] = mapped_column(str_enum(AnalysisParty, "analysis_party"), nullable=False)
    party_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    categories: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)
    depth: Mapped[AnalysisDepth] = mapped_column(str_enum(AnalysisDepth, "analysis_depth"), nullable=False)
    model_config_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_configs.id", ondelete="SET NULL"), nullable=True
    )
    # Snapshot конфигурации на момент запуска — без секретов.
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    cloud_transfer: Mapped[bool] = mapped_column(Boolean, nullable=False)
    cloud_consent_at: Mapped[datetime | None] = tz_datetime(nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    progress: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    coverage: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message_safe: Mapped[str | None] = mapped_column(String(500), nullable=True)
    started_at: Mapped[datetime | None] = tz_datetime(nullable=True)
    finished_at: Mapped[datetime | None] = tz_datetime(nullable=True)
    cancel_requested_at: Mapped[datetime | None] = tz_datetime(nullable=True)
    # Результаты этапов вне чанков (структура, межраздельный проход, отметки этапов) — для продолжения
    # после сбоя. Содержит фрагменты ответа модели: наружу отдаётся только через схемы API.
    stage_results: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        UniqueConstraint("created_by_id", "idempotency_key"),
        Index("ix_analyses_created_by_id_status", "created_by_id", "status"),
        CheckConstraint(
            "NOT cloud_transfer OR cloud_consent_at IS NOT NULL", name="cloud_transfer_requires_consent"
        ),
    )


class AnalysisChunk(UUIDPk, Timestamps, Base):
    __tablename__ = "analysis_chunks"

    analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analyses.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_key: Mapped[str] = mapped_column(String(64), nullable=False)
    fragment_seq_start: Mapped[int] = mapped_column(Integer, nullable=False)
    fragment_seq_end: Mapped[int] = mapped_column(Integer, nullable=False)
    overlap_from_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[ChunkStatus] = mapped_column(
        str_enum(ChunkStatus, "chunk_status"), nullable=False, server_default=ChunkStatus.pending.value
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("analysis_id", "chunk_index"),
        UniqueConstraint("analysis_id", "chunk_key"),
    )


class AnalysisRisk(UUIDPk, Timestamps, Base):
    __tablename__ = "analysis_risks"

    analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analyses.id", ondelete="CASCADE"), nullable=False
    )
    risk_key: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[RiskKind] = mapped_column(str_enum(RiskKind, "risk_kind"), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[RiskSeverity] = mapped_column(str_enum(RiskSeverity, "risk_severity"), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    consequences: Mapped[str] = mapped_column(Text, nullable=False)
    affected_party: Mapped[AffectedParty] = mapped_column(
        str_enum(AffectedParty, "affected_party"), nullable=False
    )
    recommendation: Mapped[str] = mapped_column(Text, nullable=False)
    suggested_revision: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    validation_status: Mapped[ValidationStatus] = mapped_column(
        str_enum(ValidationStatus, "validation_status"),
        nullable=False,
        server_default=ValidationStatus.pending.value,
    )
    validation_notes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    search_scope: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    source_chunk_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=text("'{}'")
    )
    dedup_group: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Код правила организации (этап 11), проверенный по snapshot анализа; текст правила — в snapshot.
    rule_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_hidden_by_user: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    __table_args__ = (
        UniqueConstraint("analysis_id", "risk_key"),
        CheckConstraint(
            "model_confidence IS NULL OR (model_confidence >= 0 AND model_confidence <= 1)",
            name="model_confidence_range",
        ),
        Index("ix_analysis_risks_analysis_id_severity", "analysis_id", "severity"),
    )


class RiskEvidence(UUIDPk, CreatedAt, Base):
    __tablename__ = "risk_evidence"

    risk_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis_risks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # NULL допустим только если модель сослалась на несуществующий фрагмент (match_status=fragment_mismatch).
    fragment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_fragments.id", ondelete="CASCADE"), nullable=True
    )
    claimed_fragment_key: Mapped[str] = mapped_column(String(32), nullable=False)
    fragment_ids_span: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=text("'{}'")
    )
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    quote_normalized: Mapped[str | None] = mapped_column(Text, nullable=True)
    match_status: Mapped[MatchStatus] = mapped_column(
        str_enum(MatchStatus, "match_status"), nullable=False, server_default=MatchStatus.pending.value
    )
    match_char_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    match_char_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clause_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    relevance_status: Mapped[RelevanceStatus] = mapped_column(
        str_enum(RelevanceStatus, "relevance_status"),
        nullable=False,
        server_default=RelevanceStatus.unchecked.value,
    )
    relevance_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    relevance_checked_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    verifier_model_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # 0 — доказательство из анализа; 1 — из повторного запроса точной цитаты (заменяет попытку 0).
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        CheckConstraint(
            "fragment_id IS NOT NULL OR match_status IN ('fragment_mismatch', 'pending')",
            name="fragment_required_for_match",
        ),
    )
