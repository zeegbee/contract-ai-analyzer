"""Адаптер OpenRouter (OpenAI-совместимый API).

Сверено с документацией OpenRouter (openrouter.ai/docs, 2026-10-16):
- `POST {base}/chat/completions`, `Authorization: Bearer <key>`; `response_format`
  `{"type": "json_schema", "json_schema": {"name", "strict", "schema"}}` и
  `provider.require_parameters=true`, чтобы маршрутизировать только на эндпоинты с поддержкой схем;
- ошибки — `{"error": {"code", "message", "metadata"}}`; после начала генерации ошибка может прийти
  в теле ответа с HTTP 200 (`error` и/или `finish_reason: "error"`);
- `reasoning: {"effort": "none"}` выключает рассуждения; без параметра — режим модели по умолчанию;
- `usage` (`prompt_tokens`, `completion_tokens`, `total_tokens`, `cost`) — cost берётся только если есть;
- `GET {base}/models` — `data[]` с `context_length`, `top_provider.max_completion_tokens`,
  `pricing.prompt/completion` (строки, USD за токен), `supported_parameters`;
- `GET {base}/key` — сведения о ключе (проверка соединения и ключа без генерации).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.llm.base import (
    ChatMessage,
    ProviderHealth,
    RemoteModelInfo,
    StructuredRequest,
    StructuredResult,
    Usage,
)
from app.llm.errors import (
    ContextTooLong,
    InvalidModelOutput,
    ModelUnavailable,
    ProviderError,
    ProviderUnavailable,
)
from app.llm.http import ProviderHttp, RawResponse, map_status
from app.llm.output import as_dict, parse_json_object, schema_instruction
from app.models.enums import StructuredOutputSupport

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
MODELS_MAX_BYTES = 32 * 1024 * 1024
MTOK = Decimal(1_000_000)
_NO_HEADERS = httpx.Headers()

_CONTEXT_MARKERS = (
    "context length",
    "context_length",
    "maximum context",
    "context window",
    "too many tokens",
)
_MODEL_MARKERS = ("not a valid model", "no endpoints found", "model not found", "no allowed providers")


def _error_message(body: Any) -> str:
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return str(body["error"].get("message") or "").lower()
    return ""


def _map_error(raw: RawResponse) -> ProviderError:
    # Сообщение провайдера используется только для классификации и наружу не передаётся.
    message = _error_message(raw.json())
    if raw.status in (400, 413, 422) and any(m in message for m in _CONTEXT_MARKERS):
        return ContextTooLong()
    if raw.status in (400, 404) and any(m in message for m in _MODEL_MARKERS):
        return ModelUnavailable()
    return map_status(raw)


def _in_body_error(error: Any) -> ProviderError:
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(code, int) and 400 <= code < 600:
        return _map_error(RawResponse(status=code, headers=_NO_HEADERS, body=b""))
    return ProviderUnavailable("Провайдер вернул ошибку во время генерации", code="generation_failed")


def _price_per_mtok(value: Any) -> Decimal | None:
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    # Отрицательные значения у OpenRouter означают «цена определяется маршрутизацией» — неизвестна.
    if not price.is_finite() or price < 0:
        return None
    return (price * MTOK).normalize()


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _usage(data: Any) -> Usage | None:
    if not isinstance(data, dict):
        return None
    cost: Decimal | None = None
    raw_cost = data.get("cost")
    if isinstance(raw_cost, int | float) and not isinstance(raw_cost, bool):
        cost = Decimal(str(raw_cost))
    return Usage(
        prompt_tokens=_int_or_none(data.get("prompt_tokens")),
        completion_tokens=_int_or_none(data.get("completion_tokens")),
        total_tokens=_int_or_none(data.get("total_tokens")),
        cost=cost,
        cost_currency="USD" if cost is not None else None,  # OpenRouter считает в кредитах = USD
    )


def _messages(messages: list[ChatMessage]) -> list[dict[str, str]]:
    return [{"role": m.role, "content": m.content} for m in messages]


class OpenRouterProvider:
    def __init__(self, http: ProviderHttp) -> None:
        self.http = http

    def build_chat_body(self, req: StructuredRequest) -> dict[str, Any]:
        messages = list(req.messages)
        body: dict[str, Any] = {"model": req.model, "stream": False}
        mode = req.structured_output
        if mode is StructuredOutputSupport.json_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": req.schema_name, "strict": True, "schema": req.json_schema},
            }
            body["provider"] = {"require_parameters": True}
        else:
            messages = [ChatMessage("system", schema_instruction(req.json_schema)), *messages]
            if mode is StructuredOutputSupport.json_mode:
                body["response_format"] = {"type": "json_object"}
                body["provider"] = {"require_parameters": True}
        body["messages"] = _messages(messages)
        p = req.params
        if p.temperature is not None:
            body["temperature"] = p.temperature
        if p.top_p is not None:
            body["top_p"] = p.top_p
        if p.seed is not None:
            body["seed"] = p.seed
        if p.max_output_tokens is not None:
            # `max_tokens` помечен в документации устаревшим в пользу `max_completion_tokens`, но именно
            # он перечислен в `supported_parameters` моделей, по которым работает require_parameters.
            body["max_tokens"] = p.max_output_tokens
        if p.reasoning == "off":
            body["reasoning"] = {"effort": "none"}
        return body

    async def generate_structured(self, req: StructuredRequest) -> StructuredResult:
        body = self.build_chat_body(req)
        return await self.http.with_retries(lambda: self._chat_once(body), op="chat")

    async def _chat_once(self, body: dict[str, Any]) -> StructuredResult:
        raw = await self.http.send("POST", "/chat/completions", json_body=body)
        if raw.status != 200:
            raise _map_error(raw)
        data = raw.json()
        if not isinstance(data, dict):
            raise ProviderUnavailable("Провайдер вернул некорректный ответ", code="invalid_provider_response")
        if data.get("error"):
            raise _in_body_error(data["error"])
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderUnavailable("Провайдер вернул пустой ответ", code="invalid_provider_response")
        choice = choices[0]
        finish = choice.get("finish_reason")
        if finish == "error":
            raise ProviderUnavailable("Генерация прервана провайдером", code="generation_failed")
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        usage = _usage(data.get("usage"))
        if finish == "length":
            raise InvalidModelOutput(
                "Ответ модели обрезан по лимиту токенов", code="output_truncated"
            ).with_usage(usage)
        try:
            parsed = parse_json_object(content)
        except InvalidModelOutput as exc:
            raise exc.with_usage(usage) from None
        return StructuredResult(
            parsed_json=parsed,
            raw_text_len=len(content) if isinstance(content, str) else 0,
            usage=usage,
            provider_request_id=str(data["id"])[:200] if data.get("id") else None,
            finish_reason=str(finish) if finish else None,
            model=str(data["model"])[:200] if data.get("model") else None,
        )

    async def check_connection(self) -> ProviderHealth:
        raw = await self.http.with_retries(lambda: self._get("/key"), op="key")
        data = raw.json()
        info = data.get("data") if isinstance(data, dict) else None
        details: dict[str, Any] = {}
        if isinstance(info, dict):
            for key in ("limit", "limit_remaining", "usage", "is_free_tier"):
                if key not in info:
                    continue
                value = info[key]
                if isinstance(value, int | float | bool) or value is None:
                    details[key] = value
        return ProviderHealth(ok=True, status="ok", message="Ключ принят провайдером", details=details)

    async def _get(self, path: str, max_bytes: int | None = None) -> RawResponse:
        if max_bytes is None:
            raw = await self.http.send("GET", path)
        else:
            raw = await self.http.send("GET", path, max_bytes=max_bytes)
        if raw.status != 200:
            raise _map_error(raw)
        return raw

    async def list_models(self) -> list[RemoteModelInfo]:
        raw = await self.http.with_retries(lambda: self._get("/models", MODELS_MAX_BYTES), op="models")
        data = raw.json()
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ProviderUnavailable(
                "Провайдер вернул некорректный список моделей", code="invalid_provider_response"
            )
        now = datetime.now(UTC)
        return [m for m in (self._model_info(item, now) for item in items) if m is not None]

    @staticmethod
    def _model_info(item: Any, now: datetime) -> RemoteModelInfo | None:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return None
        pricing = as_dict(item.get("pricing"))
        top = as_dict(item.get("top_provider"))
        params = item.get("supported_parameters")
        advertises = (
            ("structured_outputs" in params or "response_format" in params)
            if isinstance(params, list)
            else None
        )
        return RemoteModelInfo(
            external_model_id=item["id"][:200],
            display_name=str(item.get("name") or item["id"])[:200],
            context_window=_int_or_none(item.get("context_length")),
            max_output_tokens=_int_or_none(top.get("max_completion_tokens")),
            prompt_price_per_mtok=_price_per_mtok(pricing.get("prompt")),
            completion_price_per_mtok=_price_per_mtok(pricing.get("completion")),
            advertises_structured_output=advertises,
            details={"supported_parameters": [str(p) for p in params][:50]}
            if isinstance(params, list)
            else {},
            fetched_at=now,
        )

    async def describe_model(self, external_model_id: str) -> RemoteModelInfo:
        for model in await self.list_models():
            if model.external_model_id == external_model_id:
                return model
        raise ModelUnavailable("Модель не найдена в каталоге OpenRouter")
