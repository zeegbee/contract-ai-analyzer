"""Номера пунктов и заголовки, явно присутствующие в тексте (ARCHITECTURE §8.4).

`clause_ref` заполняется только если номер написан в начале фрагмента в узнаваемой форме:
«4.2.», «4.2.1», «5. Текст», «Статья 5», «Раздел 3», «Глава 2», «Пункт 7». Иначе — None.
Ничего не досчитывается и не угадывается.
"""

from __future__ import annotations

import re

# Многоуровневый номер: «4.2», «4.2.», «10.3.1». Уровни после первого — не больше двух цифр, чтобы
# суммы вида «1.500 руб.» не принимались за пункт; дата «01.02.2026» не проходит из-за длины уровня.
_MULTI_LEVEL = re.compile(r"^\s*(?:п\.\s*|пункт\s+)?(\d{1,3}(?:\.\d{1,2}){1,5})\.?(?=\s|$)", re.IGNORECASE)
# Одноуровневый номер с точкой: «5. Предмет договора».
_SINGLE_LEVEL = re.compile(r"^\s*(\d{1,3})\.(?=\s+\S)")
_NAMED = re.compile(
    r"^\s*(Статья|Раздел|Глава|Пункт|Часть|Приложение|Article|Section|Clause)\s+"
    r"(\d{1,3}(?:\.\d{1,2}){0,4}|[IVXLC]{1,6})(?=[\s.:)]|$)",
    re.IGNORECASE,
)
_HEADING_MAX_LEN = 150


def clause_ref_from_text(text: str) -> str | None:
    head = text[:80]
    if m := _NAMED.match(head):
        return f"{m.group(1).capitalize()} {m.group(2)}"
    if m := _MULTI_LEVEL.match(head):
        return m.group(1)
    if m := _SINGLE_LEVEL.match(head):
        return m.group(1)
    return None


def clause_ref_from_label(label: str) -> str | None:
    """Номер из вычисленной автонумерации DOCX («4.2.», «Статья 5.»)."""
    label = label.strip()
    if m := _NAMED.match(label):
        return f"{m.group(1).capitalize()} {m.group(2)}"
    if m := re.fullmatch(r"(\d{1,3}(?:\.\d{1,3}){0,5})[.)]?", label):
        return m.group(1)
    return None


def _mostly_upper(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    return len(letters) >= 3 and sum(ch.isupper() for ch in letters) / len(letters) >= 0.8


def looks_like_heading(text: str) -> bool:
    """Заголовок по явным признакам в тексте: «Статья 5. Цена», «5. ПРЕДМЕТ ДОГОВОРА».

    Используется для PDF и для DOCX без стилей заголовков; источник помечается в meta.
    """
    stripped = text.strip()
    if not stripped or len(stripped) > _HEADING_MAX_LEN or "\n" in stripped.rstrip():
        return False
    if _NAMED.match(stripped) and not stripped.lower().startswith(("пункт", "clause", "часть")):
        rest = _NAMED.sub("", stripped, count=1).strip(" .:")
        # «Статья 5. Цена договора» — заголовок; «Статья 5 настоящего договора применяется…» — нет.
        return len(rest) <= 100 and not rest.endswith((";", ","))
    if m := _SINGLE_LEVEL.match(stripped):
        rest = stripped[m.end() :].strip()
        return _mostly_upper(rest) and not rest.endswith((".", ";", ","))
    # Строка капсом («ПРЕДМЕТ ДОГОВОРА»); короткие аббревиатуры вроде «ИНН …» не считаются.
    letters = sum(ch.isalpha() for ch in stripped)
    return (
        letters >= 8 and _mostly_upper(stripped) and len(stripped) <= 80 and not stripped.endswith((";", ","))
    )
