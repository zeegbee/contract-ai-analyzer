"""DTO правил рисков организации (этап 11).

Текст правила попадает в промпт модели, поэтому управляющие символы (кроме перевода строки)
удаляются при приёме, а длины ограничены. Экранирование для промпта — в `app.analysis.prompts`.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.models.enums import RiskCategory, RiskRuleKind, RiskSeverity

TITLE_MAX = 200
TEXT_MAX = 1000
_SPACES = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def clean_rule_text(value: str) -> str:
    """NFC, без управляющих и форматирующих символов (перевод строки сохраняется), без лишних пробелов."""
    value = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    value = "".join(ch for ch in value if ch == "\n" or unicodedata.category(ch)[0] != "C")
    lines = [_SPACES.sub(" ", line).strip() for line in value.split("\n")]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def _required(max_length: int, *, single_line: bool = False) -> AfterValidator:
    def check(value: str) -> str:
        value = clean_rule_text(value)
        if single_line:
            value = " ".join(value.split())
        if not value:
            raise ValueError("Поле не может быть пустым")
        if len(value) > max_length:
            raise ValueError(f"Не длиннее {max_length} символов")
        return value

    return AfterValidator(check)


def _optional(max_length: int) -> AfterValidator:
    def check(value: str | None) -> str | None:
        if value is None:
            return None
        value = clean_rule_text(value)
        if len(value) > max_length:
            raise ValueError(f"Не длиннее {max_length} символов")
        return value or None

    return AfterValidator(check)


# Ограничение длины до очистки — защита от очень больших тел запроса.
RuleTitle = Annotated[str, Field(max_length=TITLE_MAX * 2), _required(TITLE_MAX, single_line=True)]
RuleText = Annotated[str, Field(max_length=TEXT_MAX * 2), _required(TEXT_MAX)]
OptRuleText = Annotated[str | None, Field(max_length=TEXT_MAX * 2), _optional(TEXT_MAX)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RiskRuleCreateIn(_Strict):
    title: RuleTitle
    category: RiskCategory
    kind: RiskRuleKind = RiskRuleKind.clause_risk
    description: RuleText
    signs: OptRuleText = None
    severity: RiskSeverity | None = None
    recommendation: OptRuleText = None
    enabled: bool = True
    default_selected: bool = True


class RiskRuleUpdateIn(_Strict):
    version: int
    title: RuleTitle | None = None
    category: RiskCategory | None = None
    kind: RiskRuleKind | None = None
    description: RuleText | None = None
    signs: OptRuleText = None
    severity: RiskSeverity | None = None
    recommendation: OptRuleText = None
    enabled: bool | None = None
    default_selected: bool | None = None


class RiskRuleVersionIn(_Strict):
    version: int


class RiskRuleOut(BaseModel):
    id: uuid.UUID
    code: str
    title: str
    category: RiskCategory
    kind: RiskRuleKind
    description: str
    signs: str | None
    severity: RiskSeverity | None
    recommendation: str | None
    enabled: bool
    default_selected: bool
    archived_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


class RiskRuleLimitsOut(BaseModel):
    catalog: int
    per_analysis: int
    text_chars: int
    prompt_chars: int


class RiskRuleListOut(BaseModel):
    items: list[RiskRuleOut]
    limits: RiskRuleLimitsOut


class AvailableRiskRuleOut(BaseModel):
    """Правило в форме анализа: без служебных полей."""

    id: uuid.UUID
    code: str
    title: str
    category: RiskCategory
    kind: RiskRuleKind
    description: str
    severity: RiskSeverity | None
    default_selected: bool


class AvailableRiskRulesOut(BaseModel):
    items: list[AvailableRiskRuleOut]
    limits: RiskRuleLimitsOut
