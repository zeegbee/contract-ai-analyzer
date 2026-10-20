"""Контракт провайдера LLM и типы запросов/ответов."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Protocol

from app.models.enums import StructuredOutputSupport

Role = Literal["system", "user", "assistant"]
# Режим рассуждений («thinking») модели: off — выключить; None — не передавать параметр
# (рассуждения включены в режиме модели по умолчанию). Уровни усилия не используются: на OpenRouter
# заданный уровень оказался медленнее режима по умолчанию (IMPLEMENTATION_STATUS, доработка 2026-10-20).
ReasoningMode = Literal["off"]


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str

    def __repr__(self) -> str:  # содержимое может быть текстом договора — не выводим
        return f"ChatMessage(role={self.role!r}, len={len(self.content)})"


@dataclass(frozen=True)
class GenerationParams:
    temperature: float | None = None
    top_p: float | None = None
    seed: int | None = None
    max_output_tokens: int | None = None
    # Только Ollama: размер контекста (num_ctx). У OpenRouter контекст задаётся моделью.
    num_ctx: int | None = None
    reasoning: ReasoningMode | None = None

    @classmethod
    def from_mapping(
        cls, params: dict[str, Any], *, max_output_tokens: int | None = None
    ) -> GenerationParams:
        return cls(
            temperature=params.get("temperature"),
            top_p=params.get("top_p"),
            seed=params.get("seed"),
            max_output_tokens=params.get("max_output_tokens", max_output_tokens),
            num_ctx=params.get("num_ctx"),
            reasoning=params.get("reasoning"),
        )


@dataclass(frozen=True)
class StructuredRequest:
    """Запрос структурированного ответа: модель должна вернуть JSON-объект по схеме."""

    model: str
    messages: list[ChatMessage]
    json_schema: dict[str, Any]
    schema_name: str
    params: GenerationParams = field(default_factory=GenerationParams)
    # Как передавать схему провайдеру — из каталога (подтверждённая поддержка).
    structured_output: StructuredOutputSupport = StructuredOutputSupport.unknown


@dataclass(frozen=True)
class Usage:
    """Расход — только значения, которые вернул провайдер; отсутствующие — None."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cost: Decimal | None = None
    cost_currency: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost": str(self.cost) if self.cost is not None else None,
            "cost_currency": self.cost_currency,
        }


@dataclass(frozen=True)
class StructuredResult:
    parsed_json: dict[str, Any]
    raw_text_len: int
    usage: Usage | None
    provider_request_id: str | None
    finish_reason: str | None
    model: str | None = None

    def __repr__(self) -> str:
        return (
            f"StructuredResult(raw_text_len={self.raw_text_len}, finish_reason={self.finish_reason!r}, "
            f"provider_request_id={self.provider_request_id!r})"
        )


@dataclass(frozen=True)
class ProviderHealth:
    ok: bool
    status: str  # ok | error code
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RemoteModelInfo:
    """Модель, о которой сообщил провайдер. Поля None — провайдер их не сообщил."""

    external_model_id: str
    display_name: str
    context_window: int | None = None
    max_output_tokens: int | None = None
    # Цена за 1 млн токенов в USD — только из API провайдера.
    prompt_price_per_mtok: Decimal | None = None
    completion_price_per_mtok: Decimal | None = None
    # Признак из метаданных провайдера; поддержка structured output подтверждается тестовым вызовом.
    advertises_structured_output: bool | None = None
    details: dict[str, Any] = field(default_factory=dict)
    fetched_at: datetime | None = None


class LLMProvider(Protocol):
    async def generate_structured(self, req: StructuredRequest) -> StructuredResult: ...

    async def check_connection(self) -> ProviderHealth: ...

    async def list_models(self) -> list[RemoteModelInfo]: ...

    async def describe_model(self, external_model_id: str) -> RemoteModelInfo:
        """Сведения об одной модели; ModelUnavailable, если провайдер её не знает."""
        ...
