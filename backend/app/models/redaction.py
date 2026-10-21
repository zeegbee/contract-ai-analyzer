"""Маскировка перед облачным анализом (этап 10): предпросмотры и таблицы соответствий анализов.

Значения (найденные данные, таблица «метка → значение») хранятся только зашифрованными (`SecretBox`);
открыто — только счётчики по категориям без значений.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Integer, LargeBinary, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAt, Timestamps, UUIDPk, str_enum, tz_datetime
from app.models.enums import RedactionMode, RedactionPreviewStatus


class RedactionPreview(UUIDPk, Timestamps, Base):
    __tablename__ = "redaction_previews"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Ревизия настроек маскировки на момент создания: изменение настроек делает предпросмотр устаревшим.
    settings_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    redaction_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[RedactionPreviewStatus] = mapped_column(
        str_enum(RedactionPreviewStatus, "redaction_preview_status"),
        nullable=False,
        server_default=RedactionPreviewStatus.scanning.value,
    )
    # Найденные сущности и правки пользователя (JSON, зашифрован); после запуска анализа очищается.
    entities_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    secret_key_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Категория → число значений; ручные добавления и снятия — числом. Без значений.
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mode: Mapped[RedactionMode | None] = mapped_column(
        str_enum(RedactionMode, "redaction_mode"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = tz_datetime(nullable=True)
    unmasked_confirmed_at: Mapped[datetime | None] = tz_datetime(nullable=True)
    expires_at: Mapped[datetime] = tz_datetime(nullable=False)


class AnalysisRedaction(CreatedAt, Base):
    __tablename__ = "analysis_redactions"

    analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analyses.id", ondelete="CASCADE"), primary_key=True
    )
    # Предпросмотр расходуется: одна связка с анализом.
    preview_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("redaction_previews.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )
    mode: Mapped[RedactionMode] = mapped_column(str_enum(RedactionMode, "redaction_mode"), nullable=False)
    unmasked_confirmed_at: Mapped[datetime | None] = tz_datetime(nullable=True)
    # Таблица «метка → значение и вхождения» (JSON, зашифрован); для unmasked_by_user — NULL.
    mapping_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    secret_key_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    redaction_version: Mapped[str] = mapped_column(String(32), nullable=False)
    settings_revision: Mapped[int] = mapped_column(Integer, nullable=False)
