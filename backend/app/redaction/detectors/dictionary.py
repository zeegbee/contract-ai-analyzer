"""Правила Superadmin: словарь (с учётом морфологии), регулярные выражения (RE2, линейное время) и
список исключений (никогда не маскируется)."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from app.redaction import nlp
from app.redaction.detectors.base import TIER_EXPLICIT, Hit, norm_space

# Между словами фразы допускаются только пробелы, кавычки, дефисы и т.п. (не больше 3 символов).
_MAX_GAP = 3


def _lemmas(phrase: str) -> tuple[str, ...]:
    return tuple(nlp.lemma(w) for _s, _e, w in nlp.words(phrase))


@dataclass(frozen=True)
class DictionaryEntry:
    text: str
    category: str


class Dictionary:
    def __init__(self, entries: Sequence[DictionaryEntry]) -> None:
        self.entries = [(e, _lemmas(e.text)) for e in entries]
        self.entries = [(e, lem) for e, lem in self.entries if lem]

    def find(self, text: str) -> Iterator[Hit]:
        if not self.entries:
            return
        tokens = nlp.words(text)
        lemmas = [nlp.lemma(w) for _s, _e, w in tokens]
        for entry, target in self.entries:
            n = len(target)
            for i in range(len(tokens) - n + 1):
                if tuple(lemmas[i : i + n]) != target:
                    continue
                if any(tokens[j + 1][0] - tokens[j][1] > _MAX_GAP for j in range(i, i + n - 1)):
                    continue
                start, end = tokens[i][0], tokens[i + n - 1][1]
                key = "dict:" + " ".join(target)
                yield Hit(start, end, entry.category, key, source="dictionary", tier=TIER_EXPLICIT)


@dataclass(frozen=True)
class CustomPattern:
    pattern: str
    category: str


def compile_pattern(pattern: str) -> Any:
    """Компиляция в RE2 (без обратных ссылок и lookaround — линейное время, ReDoS невозможен)."""
    import re2  # noqa: PLC0415 - нативная библиотека нужна только для пользовательских шаблонов

    return re2.compile(pattern)


class Patterns:
    def __init__(self, patterns: Sequence[CustomPattern]) -> None:
        self.compiled = [(p, compile_pattern(p.pattern)) for p in patterns]

    def find(self, text: str) -> Iterator[Hit]:
        for index, (p, rx) in enumerate(self.compiled):
            for m in rx.finditer(text):
                if m.end() > m.start():
                    key = f"custom:{index}:{norm_space(m.group(0)).lower()}"
                    yield Hit(
                        m.start(), m.end(), p.category, key, source="custom_pattern", tier=TIER_EXPLICIT
                    )


class AllowList:
    """Исключения: совпадение по тексту (без учёта регистра) или по леммам."""

    def __init__(self, items: Sequence[str]) -> None:
        self.texts = {norm_space(i).casefold() for i in items if i.strip()}
        self.lemmas = {_lemmas(i) for i in items if i.strip()} - {()}

    def allows(self, value: str) -> bool:
        if not self.texts:
            return False
        cleaned = norm_space(value.strip('«»"“”„ ')).casefold()
        return cleaned in self.texts or _lemmas(value) in self.lemmas
