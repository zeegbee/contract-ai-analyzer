"""Нормализация и поиск цитат в тексте фрагментов (ARCHITECTURE §10.1, NORMALIZER_VERSION).

Нормализация применяется одинаково к цитате и к тексту и не меняет слов:
- Unicode NFC (буква + комбинируемые знаки);
- пробельные символы (CR/LF, табуляция, NBSP U+00A0, узкие пробелы U+2009/U+202F и др.) → пробел,
  последовательности пробелов → один пробел, обрезка по краям;
- удаляются мягкий перенос U+00AD и символы нулевой ширины (U+200B–U+200D, U+2060, U+FEFF);
- классы эквивалентности: двойные кавычки «»„“”" → `"`, одинарные ‘’‚' → `'`,
  дефисы и тире ‐‑‒–—−- → `-`.
Регистр, буквы, цифры и прочие знаки препинания не меняются.

Дополнительно (только если без него цитата не найдена): перенос слова по слогам в тексте —
дефис в конце строки между строчными буквами («обяза-\\nтельства») — считается частью слова.
Сущности `&lt;`, `&gt;`, `&amp;` в цитате возвращаются к `<`, `>`, `&`: так текст экранируется в
промпте (prompt injection), и модель может процитировать экранированную форму.

Каждый символ нормализованной строки помнит диапазон исходной строки — по нему вычисляются смещения
совпадения в каноническом тексте версии.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MIN_QUOTE_CHARS = 20
MIN_QUOTE_WORDS = 4

_DROP = frozenset("\u00ad\u200b\u200c\u200d\u2060\ufeff")
_DOUBLE_QUOTES = frozenset('«»„“”"')
_SINGLE_QUOTES = frozenset("‘’‚'")
_DASHES = frozenset("‐‑‒–—−-")
# Перенос по слогам: дефис, (пробелы), перевод строки, (пробелы), затем строчная буква.
_HYPHEN_BREAK = re.compile(r"[-\u2010\u00ad][ \t]*\n[ \t]*(?=[a-zа-яё])")
_ENTITIES = (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"))


@dataclass(frozen=True)
class Normalized:
    text: str
    # Для каждого символа `text` — [начало, конец) соответствующего участка исходной строки.
    starts: tuple[int, ...]
    ends: tuple[int, ...]


def _class_char(ch: str) -> str:
    if ch in _DOUBLE_QUOTES:
        return '"'
    if ch in _SINGLE_QUOTES:
        return "'"
    if ch in _DASHES:
        return "-"
    return ch


def normalize(source: str, *, dehyphenate: bool = False) -> Normalized:
    out: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    i, n = 0, len(source)
    while i < n:
        if dehyphenate and i > 0 and source[i - 1].isalpha():
            m = _HYPHEN_BREAK.match(source, i)
            if m:
                i = m.end()
                continue
        j = i + 1
        while j < n and unicodedata.combining(source[j]):
            j += 1
        for raw in unicodedata.normalize("NFC", source[i:j]):
            if raw in _DROP:
                continue
            ch = raw
            if raw.isspace():
                if not out:
                    continue
                if out[-1] == " ":
                    ends[-1] = j
                    continue
                ch = " "
            out.append(_class_char(ch))
            starts.append(i)
            ends.append(j)
        i = j
    while out and out[-1] == " ":
        out.pop()
        starts.pop()
        ends.pop()
    return Normalized("".join(out), tuple(starts), tuple(ends))


def unescape_quote(quote: str) -> str:
    for entity, char in _ENTITIES:
        quote = quote.replace(entity, char)
    return quote


def is_too_short(normalized_quote: str) -> bool:
    """Тривиальная цитата ничего не доказывает: < 20 символов или < 4 слов."""
    return len(normalized_quote) < MIN_QUOTE_CHARS or len(normalized_quote.split(" ")) < MIN_QUOTE_WORDS


@dataclass(frozen=True)
class QuoteMatch:
    # "exact" — совпало без нормализации, "normalized" — после нормализации.
    kind: str
    start: int  # смещения в строке, в которой искали, [start, end)
    end: int
    dehyphenated: bool = False


def find_quote(haystack: str, quote: str) -> QuoteMatch | None:
    """Ищет цитату в строке: сначала дословно, затем после нормализации, затем с учётом переносов."""
    pos = haystack.find(quote)
    if pos >= 0 and quote.strip():
        return QuoteMatch("exact", pos, pos + len(quote))
    for dehyphenate in (False, True):
        needle = normalize(quote, dehyphenate=dehyphenate).text
        if not needle:
            return None
        norm = normalize(haystack, dehyphenate=dehyphenate)
        k = norm.text.find(needle)
        if k >= 0:
            return QuoteMatch("normalized", norm.starts[k], norm.ends[k + len(needle) - 1], dehyphenate)
    return None
