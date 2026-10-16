"""DTO документов, версий, страниц, фрагментов и доступа."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.enums import (
    DocumentFormat,
    DocumentPermission,
    DocumentStatus,
    ExtractionStatus,
    FragmentKind,
    PageSource,
    UserRole,
)

AccessKind = Literal["owner", "view", "analyze", "system"]


class VersionSummaryOut(BaseModel):
    id: uuid.UUID
    version_no: int
    original_filename: str
    detected_format: DocumentFormat
    size_bytes: int
    extraction_status: ExtractionStatus
    extraction_error_code: str | None
    page_count: int | None
    page_source: PageSource | None
    created_at: datetime


class VersionOut(VersionSummaryOut):
    mime_type: str
    sha256: str
    extractor_version: str | None
    warnings: list[dict[str, Any]]
    fragment_count: int
    converted_from_doc: bool
    updated_at: datetime


class DocumentOut(BaseModel):
    id: uuid.UUID
    title: str
    owner_id: uuid.UUID
    owner_display_name: str
    status: DocumentStatus
    current_version_id: uuid.UUID | None
    current_version: VersionSummaryOut | None
    created_at: datetime
    is_owner: bool
    access: AccessKind
    can_manage: bool
    can_share: bool
    can_analyze: bool


class DocumentListOut(BaseModel):
    items: list[DocumentOut]
    next_cursor: str | None


class DocumentDetailOut(DocumentOut):
    current_version_detail: VersionOut | None
    versions: list[VersionSummaryOut]


class DeletionOut(BaseModel):
    id: uuid.UUID
    status: DocumentStatus


class PageOut(BaseModel):
    page_no: int
    char_start: int
    char_end: int
    source: PageSource


class PageListOut(BaseModel):
    items: list[PageOut]
    page_source: PageSource | None
    page_count: int | None


class FragmentOut(BaseModel):
    id: uuid.UUID
    seq: int
    fragment_key: str
    kind: FragmentKind
    text: str
    char_start: int
    char_end: int
    page_start: int | None
    page_end: int | None
    clause_ref: str | None
    heading_path: list[str]
    table_ref: dict[str, Any] | None
    meta: dict[str, Any]


class FragmentListOut(BaseModel):
    items: list[FragmentOut]
    next_after_seq: int | None
    total: int


class UploadLimitsOut(BaseModel):
    max_upload_mb: int
    max_pages: int
    extensions: list[str]
    can_upload: bool


class AccessOut(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str
    role: UserRole
    permission: DocumentPermission
    granted_at: datetime


class AccessListOut(BaseModel):
    items: list[AccessOut]


class AccessGrantIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    permission: DocumentPermission
