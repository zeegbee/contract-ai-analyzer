"""Настройки маскировки (`system_settings.redaction`) — схема и проверка.

Хранятся: включённые категории, разрешение отправки без маскировки, словарь, шаблоны (RE2),
исключения, срок жизни предпросмотра. Пользовательские шаблоны проверяются при сохранении:
компилируются в RE2 (линейное время — ReDoS невозможен), не должны совпадать с пустой строкой.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.redaction.categories import BY_CODE, CODES, default_enabled

MAX_DICTIONARY = 500
MAX_PATTERNS = 30
MAX_PATTERN_CHARS = 300
MAX_ALLOW_LIST = 500
MIN_TTL_HOURS = 1
MAX_TTL_HOURS = 168

Phrase = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=200)]


class InvalidPattern(ValueError):
    pass


def check_pattern(pattern: str) -> None:
    from app.redaction.detectors.dictionary import compile_pattern  # noqa: PLC0415 - нативная библиотека

    try:
        compiled = compile_pattern(pattern)
    except Exception:
        raise InvalidPattern(
            "шаблон не поддерживается RE2 (обратные ссылки и lookaround недоступны)"
        ) from None
    if compiled.search("") is not None:
        raise InvalidPattern("шаблон совпадает с пустой строкой")


def _check_category(value: str) -> str:
    if value not in CODES or not BY_CODE[value].supported:
        raise ValueError("неизвестная категория")
    return value


class DictionaryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Phrase
    category: str

    _category = field_validator("category")(_check_category)


class PatternItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pattern: Annotated[str, StringConstraints(min_length=1, max_length=MAX_PATTERN_CHARS)]
    category: str

    _category = field_validator("category")(_check_category)


class RedactionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    categories: dict[str, bool] = Field(default_factory=default_enabled)
    # Разрешить пользователям отправлять текст в облако без маскировки (каждый раз — с подтверждением).
    allow_unmasked: bool = False
    custom_dictionary: list[DictionaryItem] = Field(default_factory=list, max_length=MAX_DICTIONARY)
    custom_patterns: list[PatternItem] = Field(default_factory=list, max_length=MAX_PATTERNS)
    allow_list: list[Phrase] = Field(default_factory=list, max_length=MAX_ALLOW_LIST)
    preview_ttl_hours: int = Field(default=24, ge=MIN_TTL_HOURS, le=MAX_TTL_HOURS)

    @field_validator("categories")
    @classmethod
    def _categories(cls, value: dict[str, bool]) -> dict[str, bool]:
        unknown = set(value) - CODES
        if unknown:
            raise ValueError("неизвестные категории")
        merged = default_enabled()
        merged.update(value)
        # Неподдерживаемую категорию включить нельзя.
        return {code: bool(on) and BY_CODE[code].supported for code, on in merged.items()}

    def enabled(self) -> frozenset[str]:
        return frozenset(code for code, on in self.categories.items() if on)

    def scan_config(self) -> dict[str, Any]:
        return {
            "enabled": sorted(self.enabled()),
            "dictionary": [{"text": d.text, "category": d.category} for d in self.custom_dictionary],
            "patterns": [{"pattern": p.pattern, "category": p.category} for p in self.custom_patterns],
            "allow_list": list(self.allow_list),
        }
