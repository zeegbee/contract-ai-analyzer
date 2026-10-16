"""Разбор JSON-ответа модели и инструкция о формате для моделей без нативной поддержки схем."""

from __future__ import annotations

import json
import re
from typing import Any

from app.llm.errors import InvalidModelOutput

_FENCE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL | re.IGNORECASE)


def as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def parse_json_object(content: object) -> dict[str, Any]:
    """Строгий разбор: весь ответ (допускается обёртка ```json) — один JSON-объект.

    Текст вокруг JSON не «вырезается» эвристиками: ответ должен соответствовать формату целиком.
    """
    if not isinstance(content, str) or not content.strip():
        raise InvalidModelOutput("Модель вернула пустой ответ", code="empty_model_output")
    text = content.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
    except ValueError:
        raise InvalidModelOutput("Ответ модели не является корректным JSON", code="invalid_json") from None
    if not isinstance(value, dict):
        raise InvalidModelOutput("Ответ модели должен быть JSON-объектом", code="invalid_json")
    return value


def schema_instruction(schema: dict[str, Any]) -> str:
    """Системная инструкция, если провайдер не принимает схему нативно (ответ всё равно валидируется)."""
    return (
        "Ответ — только один JSON-объект без пояснений и без Markdown, строго по JSON Schema:\n"
        + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    )
