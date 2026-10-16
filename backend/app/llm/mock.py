"""Детерминированный mock-провайдер для тестов (ARCHITECTURE §12).

Не регистрируется в фабрике провайдеров и не может быть выбран через API или БД: подставляется
только в тестах (`ProviderFactory(override=...)`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.llm.base import ProviderHealth, RemoteModelInfo, StructuredRequest, StructuredResult, Usage
from app.llm.errors import InvalidModelOutput, ModelUnavailable, ProviderError

Scenario = dict[str, Any] | ProviderError | Callable[[StructuredRequest], dict[str, Any]]


@dataclass
class MockProvider:
    """Ответы берутся по очереди из `scenarios`; последний повторяется, если очередь кончилась."""

    scenarios: list[Scenario] = field(default_factory=list)
    models: list[RemoteModelInfo] = field(default_factory=list)
    health: ProviderHealth | ProviderError = field(
        default_factory=lambda: ProviderHealth(ok=True, status="ok", message="mock")
    )
    # Только метаданные вызовов (модель, число сообщений, имя схемы) — для проверок в тестах.
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def generate_structured(self, req: StructuredRequest) -> StructuredResult:
        self.calls.append({"model": req.model, "messages": len(req.messages), "schema_name": req.schema_name})
        if not self.scenarios:
            raise InvalidModelOutput("mock: нет сценария", code="invalid_json")
        scenario = self.scenarios.pop(0) if len(self.scenarios) > 1 else self.scenarios[0]
        if isinstance(scenario, ProviderError):
            raise scenario
        payload = scenario(req) if callable(scenario) else scenario
        return StructuredResult(
            parsed_json=payload,
            raw_text_len=len(str(payload)),
            usage=Usage(prompt_tokens=None, completion_tokens=None),
            provider_request_id=f"mock-{len(self.calls)}",
            finish_reason="stop",
            model=req.model,
        )

    async def check_connection(self) -> ProviderHealth:
        if isinstance(self.health, ProviderError):
            raise self.health
        return self.health

    async def list_models(self) -> list[RemoteModelInfo]:
        return list(self.models)

    async def describe_model(self, external_model_id: str) -> RemoteModelInfo:
        for model in self.models:
            if model.external_model_id == external_model_id:
                return model
        raise ModelUnavailable()
