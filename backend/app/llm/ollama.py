"""Адаптер Ollama.

Сверено с документацией Ollama (docs.ollama.com, 2026-10-16):
- `POST /api/chat` — `model`, `messages`, `stream` (по умолчанию true — передаём false), `format`
  (`"json"` или JSON Schema), `options` (`temperature`, `top_p`, `seed`, `num_ctx`, `num_predict`);
  `think: false` выключает рассуждения (без параметра — режим модели по умолчанию);
  ответ — `message.content`, `done_reason`, `prompt_eval_count`, `eval_count`;
- ошибки — `{"error": "..."}`, 404 — модель не найдена (не скачана);
- `GET /api/version` — `{"version": "..."}`; `GET /api/tags` — `models[]`;
  `POST /api/show` — `model_info["<arch>.context_length"]`, `capabilities`.

Модели не скачиваются автоматически (`/api/pull` не вызывается никогда). Ключ не нужен; если
администратор задал секрет (Ollama за reverse proxy с авторизацией), он передаётся как Bearer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.llm.base import ProviderHealth, RemoteModelInfo, StructuredRequest, StructuredResult, Usage
from app.llm.errors import InvalidModelOutput, ModelUnavailable, ProviderError, ProviderUnavailable
from app.llm.http import ProviderHttp, RawResponse, map_status
from app.llm.output import as_dict, parse_json_object, schema_instruction
from app.models.enums import StructuredOutputSupport

DEFAULT_BASE_URL = "http://ollama:11434"
CHECK_TIMEOUT_SECONDS = 15.0

MODEL_NOT_PULLED = (
    "Модель не найдена в Ollama. Скачайте её администратором вручную: ollama pull <модель> "
    "(приложение не скачивает модели автоматически)"
)


def _map_error(raw: RawResponse) -> ProviderError:
    if raw.status == 404:
        return ModelUnavailable(MODEL_NOT_PULLED)
    return map_status(raw)


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class OllamaProvider:
    def __init__(self, http: ProviderHttp) -> None:
        self.http = http

    def build_chat_body(self, req: StructuredRequest) -> dict[str, Any]:
        messages = [{"role": m.role, "content": m.content} for m in req.messages]
        body: dict[str, Any] = {"model": req.model, "stream": False}
        mode = req.structured_output
        if mode is StructuredOutputSupport.json_schema:
            body["format"] = req.json_schema
        else:
            messages.insert(0, {"role": "system", "content": schema_instruction(req.json_schema)})
            if mode is not StructuredOutputSupport.none:
                body["format"] = "json"
        body["messages"] = messages
        p = req.params
        options: dict[str, Any] = {}
        if p.temperature is not None:
            options["temperature"] = p.temperature
        if p.top_p is not None:
            options["top_p"] = p.top_p
        if p.seed is not None:
            options["seed"] = p.seed
        if p.max_output_tokens is not None:
            options["num_predict"] = p.max_output_tokens
        if p.num_ctx is not None:
            options["num_ctx"] = p.num_ctx
        if options:
            body["options"] = options
        if p.reasoning == "off":
            body["think"] = False
        return body

    async def generate_structured(self, req: StructuredRequest) -> StructuredResult:
        body = self.build_chat_body(req)
        return await self.http.with_retries(lambda: self._chat_once(body), op="chat")

    async def _chat_once(self, body: dict[str, Any]) -> StructuredResult:
        raw = await self.http.send("POST", "/api/chat", json_body=body)
        if raw.status != 200:
            raise _map_error(raw)
        data = raw.json()
        if not isinstance(data, dict):
            raise ProviderUnavailable("Ollama вернула некорректный ответ", code="invalid_provider_response")
        if data.get("error"):
            raise ProviderUnavailable("Ollama вернула ошибку во время генерации", code="generation_failed")
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        reason = data.get("done_reason")
        prompt, completion = _count(data.get("prompt_eval_count")), _count(data.get("eval_count"))
        usage = Usage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion if prompt is not None and completion is not None else None,
        )
        if reason == "length":
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
            provider_request_id=None,
            finish_reason=str(reason) if reason else None,
            model=str(data["model"])[:200] if data.get("model") else None,
        )

    async def _request(self, method: str, path: str, body: Any = None) -> RawResponse:
        raw = await self.http.send(method, path, json_body=body, timeout_s=CHECK_TIMEOUT_SECONDS)
        if raw.status != 200:
            raise _map_error(raw)
        return raw

    async def check_connection(self) -> ProviderHealth:
        try:
            raw = await self.http.with_retries(lambda: self._request("GET", "/api/version"), op="version")
        except ModelUnavailable:  # 404 на /api/version — по адресу не Ollama
            raise ProviderUnavailable(
                "Адрес отвечает, но это не похоже на Ollama", code="invalid_provider_response"
            ) from None
        data = raw.json()
        version = data.get("version") if isinstance(data, dict) else None
        if not isinstance(version, str):
            raise ProviderUnavailable(
                "Адрес отвечает, но это не похоже на Ollama", code="invalid_provider_response"
            )
        return ProviderHealth(
            ok=True, status="ok", message="Ollama доступна", details={"version": version[:50]}
        )

    async def list_models(self) -> list[RemoteModelInfo]:
        raw = await self.http.with_retries(lambda: self._request("GET", "/api/tags"), op="tags")
        data = raw.json()
        items = data.get("models") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ProviderUnavailable(
                "Ollama вернула некорректный список моделей", code="invalid_provider_response"
            )
        now = datetime.now(UTC)
        result = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            details = as_dict(item.get("details"))
            result.append(
                RemoteModelInfo(
                    external_model_id=item["name"][:200],
                    display_name=item["name"][:200],
                    details={
                        k: str(details[k])[:50]
                        for k in ("family", "parameter_size", "quantization_level")
                        if details.get(k) is not None
                    },
                    fetched_at=now,
                )
            )
        return result

    async def describe_model(self, external_model_id: str) -> RemoteModelInfo:
        raw = await self.http.with_retries(
            lambda: self._request("POST", "/api/show", {"model": external_model_id}), op="show"
        )
        data = raw.json()
        if not isinstance(data, dict):
            raise ProviderUnavailable("Ollama вернула некорректный ответ", code="invalid_provider_response")
        info = as_dict(data.get("model_info"))
        context = next(
            (v for k, v in info.items() if str(k).endswith(".context_length") and isinstance(v, int)), None
        )
        caps = data.get("capabilities")
        return RemoteModelInfo(
            external_model_id=external_model_id,
            display_name=external_model_id,
            context_window=context if context and context > 0 else None,
            details={"capabilities": [str(c)[:30] for c in caps][:20]} if isinstance(caps, list) else {},
            fetched_at=datetime.now(UTC),
        )
