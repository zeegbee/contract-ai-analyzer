"""Адаптер ProxyAPI (proxyapi.ru, OpenAI-совместимый API).

Сверено с документацией ProxyAPI (proxyapi.ru/docs: api-overview, structured-outputs, reasoning,
cost-of-request, errors, rate-limits, logs; 2026-10-29):
- один базовый адрес `https://api.proxyapi.ru/v1`, `POST {base}/chat/completions`,
  `Authorization: Bearer <key>`; модель указывается целиком, с вендором (`openai/gpt-5-mini`);
- `response_format` `{"type": "json_schema", "json_schema": {"name", "strict", "schema"}}` и
  `{"type": "json_object"}`; строгий режим есть не у всех моделей (иначе 400 или текст без схемы);
  поля OpenRouter `provider.require_parameters` нет — не передаём;
- рассуждения — `reasoning_effort` (значения «от minimal до high»): «выключено» = `minimal`;
- лимит ответа — `max_completion_tokens` (по нему ProxyAPI оценивает достаточность баланса);
- `usage` без стоимости: расчёты в рублях по ценам каталога — цену задаёт администратор вручную;
- ошибки: собственные `{"detail": ...}`, вышестоящего сервиса — `{"error": {"message", ...}}`;
  20 ответов 4xx за минуту по ключу ставят ключ на паузу (429) — 4xx не повторяем (как и везде).

Каталог (документация «Модели», прислана владельцем 2026-10-29): `GET {base}/models` →
`{"object": "list", "data": [{"id", "object", "created", "owned_by"}]}`, одна модель —
`GET {base}/models/<vendor>/<model>`. Контекста и цен в ответе нет: берутся `id` и, если появятся,
`name` и целые `context_length`/`context_window`; цены задаются вручную.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from app.llm.base import ChatMessage, ProviderHealth, RemoteModelInfo, StructuredRequest
from app.llm.errors import ModelUnavailable, ProviderUnavailable
from app.llm.openrouter import MODELS_MAX_BYTES, OpenRouterProvider, _int_or_none, _messages
from app.llm.output import schema_instruction
from app.models.enums import StructuredOutputSupport

DEFAULT_BASE_URL = "https://api.proxyapi.ru/v1"
REASONING_OFF_EFFORT = "minimal"


class ProxyAPIProvider(OpenRouterProvider):
    """Разбор ответа и ошибок — как у OpenRouter (тот же формат chat/completions)."""

    def build_chat_body(self, req: StructuredRequest) -> dict[str, Any]:
        messages = list(req.messages)
        body: dict[str, Any] = {"model": req.model, "stream": False}
        mode = req.structured_output
        if mode is StructuredOutputSupport.json_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": req.schema_name, "strict": True, "schema": req.json_schema},
            }
        else:
            messages = [ChatMessage("system", schema_instruction(req.json_schema)), *messages]
            if mode is StructuredOutputSupport.json_mode:
                body["response_format"] = {"type": "json_object"}
        body["messages"] = _messages(messages)
        p = req.params
        if p.temperature is not None:
            body["temperature"] = p.temperature
        if p.top_p is not None:
            body["top_p"] = p.top_p
        if p.seed is not None:
            body["seed"] = p.seed
        if p.max_output_tokens is not None:
            body["max_completion_tokens"] = p.max_output_tokens
        if p.reasoning == "off":
            body["reasoning_effort"] = REASONING_OFF_EFFORT
        return body

    async def check_connection(self) -> ProviderHealth:
        # Баланс (`/proxyapi/balance`) по умолчанию ключам недоступен — проверяем ключ списком моделей.
        models = await self.list_models()
        return ProviderHealth(
            ok=True, status="ok", message="Ключ принят провайдером", details={"models": len(models)}
        )

    async def list_models(self) -> list[RemoteModelInfo]:
        raw = await self.http.with_retries(lambda: self._get("/models", MODELS_MAX_BYTES), op="models")
        data = raw.json()
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ProviderUnavailable(
                "Провайдер вернул некорректный список моделей", code="invalid_provider_response"
            )
        now = datetime.now(UTC)
        return [m for m in (_model_info(item, now) for item in items) if m is not None]

    async def describe_model(self, external_model_id: str) -> RemoteModelInfo:
        # 404 → ModelUnavailable (общая нормализация статусов); «/» между вендором и моделью сохраняется.
        path = "/models/" + quote(external_model_id, safe="/")
        raw = await self.http.with_retries(lambda: self._get(path), op="model")
        info = _model_info(raw.json(), datetime.now(UTC))
        if info is None:
            raise ProviderUnavailable(
                "Провайдер вернул некорректные сведения о модели", code="invalid_provider_response"
            )
        if info.external_model_id != external_model_id:
            raise ModelUnavailable("Модель не найдена в каталоге ProxyAPI")
        return info


def _model_info(item: Any, now: datetime) -> RemoteModelInfo | None:
    if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
        return None
    name = item.get("name")
    return RemoteModelInfo(
        external_model_id=item["id"][:200],
        display_name=(name if isinstance(name, str) and name else item["id"])[:200],
        context_window=_int_or_none(item.get("context_length")) or _int_or_none(item.get("context_window")),
        fetched_at=now,
    )
