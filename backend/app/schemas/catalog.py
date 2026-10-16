"""DTO провайдеров, моделей и настроек LLM.

Секрет провайдера — только во входных схемах (write-only); в ответах — `has_secret` и `secret_last4`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints

from app.models.enums import ProviderType, StructuredOutputSupport, UserRole


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelPurpose(StrEnum):
    analysis = "analysis"
    verifier = "verifier"
    structure = "structure"


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
# Идентификатор модели у провайдера: печатные символы без пробелов (например, `openai/gpt-4o`, `qwen3:8b`).
ExternalModelId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200, pattern=r"^[\x21-\x7e]+$")
]
BaseUrl = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
ApiKey = Annotated[SecretStr, Field(min_length=8, max_length=500)]
# Только назначаемые ключом вручную значения; json_schema выставляется проверкой модели.
ManualStructuredOutput = Literal["json_mode", "none", "unknown"]


# ------------------------------------------------------------------ провайдеры


class ProviderCreateIn(StrictModel):
    name: Name
    type: ProviderType
    base_url: BaseUrl | None = None
    # Для Ollama в частной сети: false — «локальный» (публичные адреса запрещены).
    is_cloud: bool | None = None
    enabled: bool = False
    allow_private_network: bool = False
    api_key: ApiKey | None = None
    timeout_s: int = Field(default=120, ge=5, le=900)
    max_concurrency: int = Field(default=2, ge=1, le=32)
    extra_headers: dict[str, str] = Field(default_factory=dict, max_length=10)


class ProviderUpdateIn(StrictModel):
    version: int
    name: Name | None = None
    base_url: BaseUrl | None = None
    is_cloud: bool | None = None
    enabled: bool | None = None
    allow_private_network: bool | None = None
    api_key: ApiKey | None = None
    clear_api_key: bool = False
    timeout_s: int | None = Field(default=None, ge=5, le=900)
    max_concurrency: int | None = Field(default=None, ge=1, le=32)
    extra_headers: dict[str, str] | None = Field(default=None, max_length=10)


class ProviderOut(BaseModel):
    id: uuid.UUID
    name: str
    type: ProviderType
    base_url: str
    is_cloud: bool
    enabled: bool
    has_secret: bool
    secret_last4: str | None
    allow_private_network: bool
    timeout_s: int
    max_concurrency: int
    extra_headers: dict[str, str]
    last_check_at: datetime | None
    last_check_status: str | None
    last_check_error_safe: str | None
    url_warnings: list[str]
    model_count: int
    version: int
    created_at: datetime
    updated_at: datetime


class ProviderListOut(BaseModel):
    items: list[ProviderOut]


class HealthOut(BaseModel):
    ok: bool
    status: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ProviderCheckOut(BaseModel):
    provider: ProviderOut
    health: HealthOut


class RemoteModelOut(BaseModel):
    external_model_id: str
    display_name: str
    context_window: int | None
    max_output_tokens: int | None
    prompt_price_per_mtok: Decimal | None
    completion_price_per_mtok: Decimal | None
    advertises_structured_output: bool | None
    details: dict[str, Any]
    already_added: bool


class RemoteModelListOut(BaseModel):
    items: list[RemoteModelOut]
    total: int
    fetched_at: datetime


class ImportModelsIn(StrictModel):
    external_model_ids: list[ExternalModelId] = Field(min_length=1, max_length=50)


# ------------------------------------------------------------------ модели


class GenerationParamsIn(StrictModel):
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    # Только Ollama: размер контекста (num_ctx).
    num_ctx: int | None = Field(default=None, ge=512, le=2_097_152)


class PricingIn(StrictModel):
    """Цена, введённая администратором вручную (за 1 млн токенов)."""

    prompt_per_mtok: Decimal = Field(ge=0, le=100_000, max_digits=12, decimal_places=6)
    completion_per_mtok: Decimal = Field(ge=0, le=100_000, max_digits=12, decimal_places=6)
    currency: Literal["USD", "RUB", "EUR"] = "USD"


class PricingOut(BaseModel):
    prompt_per_mtok: Decimal
    completion_per_mtok: Decimal
    currency: str
    # provider_api — из API провайдера на дату fetched_at; admin_manual — введено администратором.
    source: Literal["provider_api", "admin_manual"]
    fetched_at: datetime | None = None


class ModelCreateIn(StrictModel):
    provider_config_id: uuid.UUID
    external_model_id: ExternalModelId
    display_name: DisplayName
    enabled: bool = False
    purpose: list[ModelPurpose] = Field(default_factory=lambda: [ModelPurpose.analysis], min_length=1)
    priority: int = Field(default=100, ge=0, le=10_000)
    is_default: bool = False
    allowed_roles: list[UserRole] = Field(
        default_factory=lambda: [UserRole.superadmin, UserRole.admin, UserRole.analyst]
    )
    context_window: int | None = Field(default=None, ge=512, le=10_000_000)
    max_output_tokens: int | None = Field(default=None, ge=16, le=1_000_000)
    generation_params: GenerationParamsIn = Field(default_factory=GenerationParamsIn)
    structured_output: ManualStructuredOutput = "unknown"
    pricing: PricingIn | None = None
    notes: Annotated[str, StringConstraints(max_length=2000)] | None = None


class ModelUpdateIn(StrictModel):
    """Частичное изменение: учитываются только переданные поля (null — очистить, где допустимо)."""

    version: int
    display_name: DisplayName | None = None
    enabled: bool | None = None
    purpose: list[ModelPurpose] | None = Field(default=None, min_length=1)
    priority: int | None = Field(default=None, ge=0, le=10_000)
    is_default: bool | None = None
    allowed_roles: list[UserRole] | None = None
    context_window: int | None = Field(default=None, ge=512, le=10_000_000)
    max_output_tokens: int | None = Field(default=None, ge=16, le=1_000_000)
    generation_params: GenerationParamsIn | None = None
    structured_output: ManualStructuredOutput | None = None
    pricing: PricingIn | None = None
    notes: Annotated[str, StringConstraints(max_length=2000)] | None = None


class ModelOut(BaseModel):
    id: uuid.UUID
    provider_config_id: uuid.UUID
    provider_name: str
    provider_type: ProviderType
    provider_enabled: bool
    is_cloud: bool
    display_name: str
    external_model_id: str
    enabled: bool
    purpose: list[str]
    priority: int
    is_default: bool
    allowed_roles: list[str]
    context_window: int | None
    max_output_tokens: int | None
    generation_params: dict[str, Any]
    structured_output: StructuredOutputSupport
    pricing: PricingOut | None
    notes: str | None
    provider_metadata: dict[str, Any] | None
    last_tested_at: datetime | None
    last_test_status: str | None
    last_test_error_safe: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class ModelListOut(BaseModel):
    items: list[ModelOut]


class ModelTestIn(StrictModel):
    # true — дополнительно короткий тестовый запрос генерации (у облачного провайдера платный).
    generate: bool = False


class TestStep(BaseModel):
    name: str
    ok: bool
    code: str
    message: str


class ModelTestOut(BaseModel):
    model: ModelOut
    ok: bool
    steps: list[TestStep]


# ------------------------------------------------------------------ выбор модели пользователем


class CloudNoticeOut(BaseModel):
    version: str
    text: str


class AvailableModelOut(BaseModel):
    id: uuid.UUID
    display_name: str
    provider_name: str
    provider_type: ProviderType
    is_cloud: bool
    is_default: bool
    purpose: list[str]
    context_window: int | None
    pricing: PricingOut | None
    # Пользователь ещё не подтвердил текущую версию предупреждения для этого провайдера.
    consent_required: bool


class AvailableModelsOut(BaseModel):
    items: list[AvailableModelOut]
    cloud_models_enabled: bool
    cloud_notice: CloudNoticeOut


class CloudConsentIn(StrictModel):
    notice_version: Annotated[str, StringConstraints(min_length=1, max_length=32)]


class CloudConsentOut(BaseModel):
    provider_config_id: uuid.UUID
    notice_version: str
    accepted_at: datetime


# ------------------------------------------------------------------ настройки и история


class LlmSettingsOut(BaseModel):
    cloud_models_enabled: bool
    cloud_notice: CloudNoticeOut
    can_edit: bool


class LlmSettingsIn(StrictModel):
    cloud_models_enabled: bool | None = None
    cloud_notice_text: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=20, max_length=4000)] | None
    ) = None


class ConfigRevisionOut(BaseModel):
    id: uuid.UUID
    entity_type: str
    entity_id: str
    revision: int
    diff: dict[str, Any]
    changed_by_id: uuid.UUID | None
    created_at: datetime


class ConfigRevisionListOut(BaseModel):
    items: list[ConfigRevisionOut]
