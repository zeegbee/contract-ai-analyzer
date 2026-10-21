"""DTO маскировки: настройки, предпросмотр, правки, подтверждение."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.redaction.settings import RedactionSettings


class CategoryOut(BaseModel):
    code: str
    title: str
    short: str
    group: str
    method: str
    reliability: str
    label: str
    default_enabled: bool
    warning: str | None
    supported: bool


class RedactionSettingsOut(BaseModel):
    settings: RedactionSettings
    revision: int
    updated_at: datetime | None
    updated_by_id: uuid.UUID | None
    can_manage: bool
    categories: list[CategoryOut]


class RedactionSettingsIn(RedactionSettings):
    # Явное подтверждение Superadmin при включении отправки без маскировки.
    confirm_allow_unmasked: bool = False


class RedactionConfigOut(BaseModel):
    """Что нужно экрану предпросмотра любому пользователю (без словарей и шаблонов)."""

    allow_unmasked: bool
    unmasked_notice_version: str
    preview_ttl_hours: int
    categories: list[CategoryOut]


class OccurrenceOut(BaseModel):
    id: str
    fragment: str
    start: int
    end: int
    text: str
    source: str
    masked: bool


class EntityOut(BaseModel):
    id: str
    category: str
    canonical: str
    confidence: str
    source: str
    masked: bool
    # Метка, которую увидит модель (null — значение не маскируется).
    label: str | None
    occurrences: list[OccurrenceOut]


class PreviewOut(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    status: str
    error_code: str | None
    error_message: str | None
    created_at: datetime
    expires_at: datetime
    confirmed_at: datetime | None
    mode: str | None
    # Предпросмотр устарел: настройки изменились (`settings_changed`) или истёк срок (`expired`).
    stale: bool
    stale_reason: str | None
    used: bool
    counts: dict[str, Any]
    ner_available: bool | None
    allow_unmasked: bool
    entities: list[EntityOut]


class SetEntityOp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["set_entity"]
    entity_id: Annotated[str, Field(max_length=32)]
    masked: bool


class SetOccurrenceOp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["set_occurrence"]
    occurrence_id: Annotated[str, Field(max_length=40)]
    masked: bool


class SetCategoryOp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["set_category"]
    entity_id: Annotated[str, Field(max_length=32)]
    category: Annotated[str, Field(max_length=64)]


class AddMarkOp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["add"]
    fragment: Annotated[str, Field(max_length=32)]
    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(ge=1)]
    category: Annotated[str, Field(max_length=64)]
    # Найти и пометить все вхождения того же текста во всём документе.
    all_occurrences: bool = False


EditOp = Annotated[SetEntityOp | SetOccurrenceOp | SetCategoryOp | AddMarkOp, Field(discriminator="op")]


class PreviewEditIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ops: Annotated[list[EditOp], Field(min_length=1, max_length=200)]


class PreviewConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["masked", "unmasked"] = "masked"
    # Отметка «Подсвеченные данные проверены» — обязательна.
    reviewed: bool = False
    # Для mode=unmasked: отдельное подтверждение и версия текста предупреждения.
    confirm_unmasked: bool = False
    unmasked_notice_version: Annotated[str | None, Field(max_length=32)] = None
