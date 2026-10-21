"""Общие типы и помощники детекторов."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

# Уровни при разрешении пересечений: меньше — важнее.
TIER_EXPLICIT = 0  # ручные пометки, словарь и шаблоны Superadmin
TIER_STRONG = 1  # контрольные суммы и точные форматы
TIER_CONTEXT = 2  # контекстные шаблоны, NER
TIER_WEAK = 3  # словарные пометки «на проверку», распространённые формы

CONFIDENCE_RANK = {"high": 2, "medium": 1, "low": 0}


@dataclass(frozen=True)
class Hit:
    start: int
    end: int
    category: str
    # Ключ связывания: вхождения с одинаковым ключом — одна сущность (одна метка).
    key: str
    source: str = "pattern"
    confidence: str = "high"
    tier: int = TIER_STRONG
    # Дополнительные признаки (например, контекст подписанта для ФИО).
    flags: frozenset[str] = field(default_factory=frozenset)

    @property
    def length(self) -> int:
        return self.end - self.start


def before(text: str, start: int, width: int) -> str:
    return text[max(0, start - width) : start].lower()


def after(text: str, end: int, width: int) -> str:
    return text[end : end + width].lower()


def norm_space(value: str) -> str:
    return " ".join(value.split())


def digits_of(value: str) -> str:
    return "".join(c for c in value if c.isdigit())


def strip_span(text: str, start: int, end: int, chars: str = " \t\n,.;:") -> tuple[int, int]:
    while start < end and text[start] in chars:
        start += 1
    while end > start and text[end - 1] in chars:
        end -= 1
    return start, end


MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}
DATE_RE = re.compile(
    r"(?<![\d.])(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{4}|\d{2})(?![\d])(?:\s*(?:г\.|года?\b))?"
    r"|«?(?P<d2>\d{1,2})»?\s+(?P<mon>" + "|".join(MONTHS) + r")\s+(?P<y2>\d{4})(?:\s*(?:г\.|года?\b))?"
    r"|(?<![\d-])(?P<y3>\d{4})-(?P<m3>\d{2})-(?P<d3>\d{2})(?![\d-])",
    re.IGNORECASE,
)


def parse_date(m: re.Match[str]) -> date | None:
    try:
        if m.group("d"):
            year = int(m.group("y"))
            if year < 100:
                year += 2000 if year < 50 else 1900
            return date(year, int(m.group("m")), int(m.group("d")))
        if m.group("d2"):
            return date(int(m.group("y2")), MONTHS[m.group("mon").lower()], int(m.group("d2")))
        return date(int(m.group("y3")), int(m.group("m3")), int(m.group("d3")))
    except ValueError:
        return None
