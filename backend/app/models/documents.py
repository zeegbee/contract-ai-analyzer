"""Документы, версии, страницы и фрагменты."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPk, str_enum, tz_datetime
from app.models.enums import DocumentFormat, DocumentStatus, ExtractionStatus, FragmentKind, PageSource


class Document(UUIDPk, Timestamps, Base):
    __tablename__ = "documents"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[DocumentStatus] = mapped_column(
        str_enum(DocumentStatus, "document_status"),
        nullable=False,
        server_default=DocumentStatus.active.value,
    )
    deleted_at: Mapped[datetime | None] = tz_datetime(nullable=True)
    # use_alter: циклическая ссылка documents ↔ document_versions
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    # Idempotency-Key загрузки: повтор запроса после обрыва связи возвращает тот же документ.
    upload_idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        Index(
            "uq_documents_owner_upload_idempotency_key",
            "owner_id",
            "upload_idempotency_key",
            unique=True,
            postgresql_where=sql_text("upload_idempotency_key IS NOT NULL"),
        ),
    )


class DocumentVersion(UUIDPk, Timestamps, Base):
    __tablename__ = "document_versions"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    detected_format: Mapped[DocumentFormat] = mapped_column(
        str_enum(DocumentFormat, "document_format"), nullable=False
    )
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    converted_storage_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    canonical_text_storage_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    extraction_status: Mapped[ExtractionStatus] = mapped_column(
        str_enum(ExtractionStatus, "extraction_status"),
        nullable=False,
        server_default=ExtractionStatus.pending.value,
    )
    extraction_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    extractor_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_source: Mapped[PageSource | None] = mapped_column(str_enum(PageSource, "page_source"), nullable=True)
    warnings: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb"))
    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (UniqueConstraint("document_id", "version_no"),)


class DocumentPage(UUIDPk, Base):
    __tablename__ = "document_pages"

    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False
    )
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[PageSource] = mapped_column(str_enum(PageSource, "page_source"), nullable=False)

    __table_args__ = (UniqueConstraint("version_id", "page_no"),)


class DocumentFragment(UUIDPk, Base):
    """Неизменяемый после извлечения фрагмент текста версии документа."""

    __tablename__ = "document_fragments"

    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    fragment_key: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[FragmentKind] = mapped_column(str_enum(FragmentKind, "fragment_kind"), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    # Страницы и пункт — только если достоверно определены, иначе NULL.
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clause_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    heading_path: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=sql_text("'[]'::jsonb")
    )
    table_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sql_text("'{}'::jsonb")
    )

    __table_args__ = (
        UniqueConstraint("version_id", "seq"),
        UniqueConstraint("version_id", "fragment_key"),
        Index("ix_document_fragments_version_id_page_start", "version_id", "page_start"),
    )
