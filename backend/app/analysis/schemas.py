"""Схемы ответов модели (ARCHITECTURE §9.3, SCHEMA_VERSION).

Ответ валидируется Pydantic: `extra="forbid"` (поля вне схемы — ошибка), перечисления, ограничения
длины. В схеме нет страниц, пунктов, статусов проверки и прав — их вычисляет сервер.

Провайдеру передаётся упрощённая JSON Schema (`provider_schema`): ссылки раскрыты, все поля
обязательны (nullable — через `null`), `additionalProperties: false`; ограничения длины и диапазоны
не передаются (их поддерживают не все провайдеры в strict-режиме) — их проверяет сервер.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints

from app.models.enums import AffectedParty, RelevanceStatus, RiskCategory, RiskKind, RiskSeverity

FRAGMENT_KEY_PATTERN = r"^F\d{6}$"

FragmentKey = Annotated[
    str,
    StringConstraints(pattern=FRAGMENT_KEY_PATTERN),
    Field(description="Значение атрибута id фрагмента, например F000123"),
]


def _text(max_length: int) -> Any:
    return StringConstraints(strip_whitespace=True, min_length=1, max_length=max_length)


def _optional(max_length: int) -> Any:
    return StringConstraints(strip_whitespace=True, max_length=max_length)


Text200 = Annotated[str, _text(200)]
Text300 = Annotated[str, _text(300)]
Text600 = Annotated[str, _text(600)]
Text1500 = Annotated[str, _text(1500)]
Text2000 = Annotated[str, _text(2000)]
OptText100 = Annotated[str | None, _optional(100)]
OptText200 = Annotated[str | None, _optional(200)]
OptText500 = Annotated[str | None, _optional(500)]
OptText1000 = Annotated[str | None, _optional(1000)]
OptText3000 = Annotated[str | None, _optional(3000)]


RULE_CODE_MAX = 64


def _lenient_code(value: Any) -> str | None:
    """Код правила из ответа модели: не строка или пусто → None; длина обрезается (проверка — сервером)."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:RULE_CODE_MAX] or None


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceItem(_Strict):
    fragment_id: FragmentKey
    quote: Text1500 = Field(description="Дословная непрерывная цитата из текста этого фрагмента")


class RiskItem(_Strict):
    title: Text200 = Field(description="Краткое название риска")
    category: RiskCategory
    kind: RiskKind
    severity: RiskSeverity
    description: Text2000 = Field(description="Описание риска")
    reason: Text2000 = Field(description="Почему это риск: какое условие и как к нему приводит")
    consequences: Text2000 = Field(description="Возможные последствия")
    affected_party: AffectedParty
    recommendation: Text2000 = Field(description="Рекомендация")
    suggested_revision: OptText3000 = Field(description="Проект редакции условия для обсуждения или null")
    model_confidence: Annotated[float, Field(ge=0, le=1, description="Субъективная уверенность 0..1")]
    evidence: Annotated[list[EvidenceItem], Field(max_length=5)]
    search_scope: OptText1000 = Field(description="Для missing_clause: какие разделы просмотрены; иначе null")
    # Этап 11. Значение по умолчанию — для результатов чанков, сохранённых до появления поля и для
    # проходов без правил (поле убирается из схемы провайдера, см. `risk_list_schema`). Посторонний код
    # не делает ответ невалидным: его снимает сервер по списку правил из snapshot анализа.
    rule_code: Annotated[str | None, BeforeValidator(_lenient_code)] = Field(
        default=None,
        description="Код правила организации из <org_rules>, которому соответствует риск, или null",
    )


class RiskList(_Strict):
    """Ответ анализа чанка и межраздельного прохода."""

    risks: Annotated[list[RiskItem], Field(max_length=30)]
    notes: OptText1000 = Field(description="Замечания о тексте или null")


class PartyItem(_Strict):
    label: Literal["party_a", "party_b", "other"]
    name: Text300 = Field(description="Наименование стороны, как в тексте")
    role: OptText100 = Field(description="Роль в договоре (Заказчик, Исполнитель…) или null")


class StructureOut(_Strict):
    contract_type: OptText200 = Field(description="Вид договора или null")
    subject: OptText1000 = Field(description="Предмет договора кратко или null")
    parties: Annotated[list[PartyItem], Field(max_length=6)]


class VerdictItem(_Strict):
    evidence_no: Annotated[int, Field(ge=1, le=20)]
    verdict: Literal["supports", "partially_supports", "does_not_support"]
    rationale: Text600 = Field(description="Краткое обоснование")

    @property
    def status(self) -> RelevanceStatus:
        return RelevanceStatus(self.verdict)


class VerifierOut(_Strict):
    verdicts: Annotated[list[VerdictItem], Field(max_length=20)]


class QuoteRetryOut(_Strict):
    withdraw: Annotated[bool, Field(description="true — риск не подтверждается текстом и отзывается")]
    evidence: Annotated[list[EvidenceItem], Field(max_length=5)]
    comment: OptText500 = Field(description="Пояснение или null")


# ------------------------------------------------------------------ JSON Schema для провайдера

_KEEP = frozenset(
    {"type", "properties", "required", "additionalProperties", "items", "enum", "anyOf", "description"}
)


def _resolve(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_resolve(v, defs) for v in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        target = defs[node["$ref"].rsplit("/", 1)[-1]]
        merged = {**copy.deepcopy(target), **{k: v for k, v in node.items() if k != "$ref"}}
        return _resolve(merged, defs)
    if "const" in node:  # Literal из одного значения
        node = {**{k: v for k, v in node.items() if k != "const"}, "enum": [node["const"]]}
    result: dict[str, Any] = {}
    for key, value in node.items():
        if key not in _KEEP:
            continue
        if key == "properties":
            result[key] = {name: _resolve(prop, defs) for name, prop in value.items()}
        else:
            result[key] = _resolve(value, defs)
    if result.get("type") == "object" or "properties" in result:
        result["type"] = "object"
        result["required"] = list(result.get("properties", {}))
        result["additionalProperties"] = False
    return result


def provider_schema(model: type[BaseModel]) -> dict[str, Any]:
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})
    schema: dict[str, Any] = _resolve(raw, defs)
    return schema


def risk_list_schema(rule_codes: Sequence[str]) -> dict[str, Any]:
    """Схема `RiskList` для провайдера с учётом правил организации (этап 11).

    Без правил в проходе поле `rule_code` убирается (схема — как до этапа 11: иначе модель заполняет
    его выдуманными значениями); с правилами — только коды правил прохода или null.
    """
    schema = provider_schema(RiskList)
    item = schema["properties"]["risks"]["items"]
    if not rule_codes:
        item["properties"].pop("rule_code", None)
        item["required"] = [name for name in item["required"] if name != "rule_code"]
        return schema
    item["properties"]["rule_code"] = {
        "anyOf": [{"type": "string", "enum": list(rule_codes)}, {"type": "null"}],
        "description": "Код правила организации из <org_rules>, которому соответствует риск, или null",
    }
    return schema
