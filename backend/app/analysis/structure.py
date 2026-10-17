"""Структура договора: разделы (детерминированно) и ключевые условия для межраздельного прохода.

Разделы строятся без модели — по фрагментам-заголовкам и коротким пунктам с явным номером верхнего
уровня («1. Предмет договора», «Статья 5», «Раздел III»), иначе — условными блоками по 40 фрагментов.
Модель дополняет структуру видом договора, предметом и сторонами (`StructureOut`); наименование
стороны принимается, только если оно найдено в тексте договора (иначе помечается
`found_in_text=false` и не используется).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from app.analysis.evidence import FragmentRef
from app.analysis.quotes import normalize
from app.analysis.schemas import StructureOut

BLOCK_SIZE = 40
# Пункт верхнего уровня считается заголовком раздела, только если он короткий.
MAX_TITLE_CHARS = 200
_TOP_LEVEL_CLAUSE = re.compile(r"^(\d{1,3}\.?|(статья|раздел|глава)\s+[\dIVXLC]+\.?)$", re.IGNORECASE)
# Ключевые условия для поиска противоречий: сроки, суммы, ответственность, расторжение, право.
_KEY_TERMS = re.compile(
    r"(срок|дн(ей|я)\b|календарн|рабоч\w+ дн|месяц|оплат|стоимост|цен[аы]|руб|%|процент"
    r"|неустойк|штраф|пен[ия]|ответственност|убытк|расторж|односторонн|отказ\w* от|подсудн"
    r"|арбитраж|применим\w+ прав|претензи"
    r"|приемк|приёмк|гаранти|конфиденц|персональн)",
    re.IGNORECASE,
)


def build_sections(fragments: Sequence[FragmentRef]) -> list[dict[str, Any]]:
    if not fragments:
        return []
    # Начало раздела — заголовок или короткий пункт верхнего уровня («1. Предмет договора»): в PDF
    # заголовки разделов часто извлекаются как обычные абзацы с номером.
    starts = [
        f
        for f in fragments
        if f.kind == "heading"
        or (f.clause_ref and _TOP_LEVEL_CLAUSE.match(f.clause_ref.strip()) and len(f.text) <= MAX_TITLE_CHARS)
    ]
    source = "headings"
    if not starts:
        sections = []
        for i in range(0, len(fragments), BLOCK_SIZE):
            block = fragments[i : i + BLOCK_SIZE]
            sections.append(
                {
                    "title": f"Фрагменты {block[0].key}–{block[-1].key}",
                    "start_seq": block[0].seq,
                    "end_seq": block[-1].seq,
                    "start_key": block[0].key,
                    "clause_ref": None,
                    "source": "blocks",
                }
            )
        return sections
    sections = []
    first_seq = fragments[0].seq
    if starts[0].seq > first_seq:
        sections.append(
            {
                "title": "Преамбула",
                "start_seq": first_seq,
                "end_seq": starts[0].seq - 1,
                "start_key": fragments[0].key,
                "clause_ref": None,
                "source": source,
            }
        )
    last_seq = fragments[-1].seq
    for i, start in enumerate(starts):
        end = starts[i + 1].seq - 1 if i + 1 < len(starts) else last_seq
        sections.append(
            {
                "title": start.text[:200],
                "start_seq": start.seq,
                "end_seq": end,
                "start_key": start.key,
                "clause_ref": start.clause_ref,
                "source": source,
            }
        )
    return sections


def outline(
    fragments: Sequence[FragmentRef], sections: Sequence[dict[str, Any]], limit: int = 150
) -> list[FragmentRef]:
    """Фрагменты-начала разделов (заголовки) — для промптов структуры и межраздельного прохода."""
    by_seq = {f.seq: f for f in fragments}
    result = [
        by_seq[s["start_seq"]] for s in sections if s["source"] != "blocks" and s["start_seq"] in by_seq
    ]
    return result[:limit]


def key_fragments(
    fragments: Sequence[FragmentRef], budget_chars: int, exclude: set[int]
) -> list[FragmentRef]:
    """Фрагменты с ключевыми условиями в порядке текста, пока хватает бюджета."""
    picked: list[FragmentRef] = []
    used = 0
    for f in fragments:
        if f.seq in exclude or f.kind == "heading" or not _KEY_TERMS.search(f.text):
            continue
        if used + len(f.text) + 2 > budget_chars:
            continue
        picked.append(f)
        used += len(f.text) + 2
    return picked


def edge_fragments(
    fragments: Sequence[FragmentRef], head_chars: int = 4000, tail_chars: int = 2000
) -> list[FragmentRef]:
    """Начало (преамбула со сторонами) и конец (реквизиты) договора для определения сторон."""
    head: list[FragmentRef] = []
    used = 0
    for f in fragments:
        if used + len(f.text) > head_chars and head:
            break
        head.append(f)
        used += len(f.text)
    tail: list[FragmentRef] = []
    used = 0
    for f in reversed(fragments):
        if f in head or (used + len(f.text) > tail_chars and tail):
            break
        tail.insert(0, f)
        used += len(f.text)
    return head + tail


def merge_llm_structure(
    base: dict[str, Any], llm: StructureOut | None, fragments: Sequence[FragmentRef]
) -> dict[str, Any]:
    result = dict(base)
    if llm is None:
        return result
    haystack = normalize(" ".join(f.text for f in fragments)).text.lower()
    result["contract_type"] = llm.contract_type
    result["subject"] = llm.subject
    result["parties"] = [
        {
            "label": p.label,
            "name": p.name,
            "role": p.role,
            "found_in_text": bool(normalize(p.name).text) and normalize(p.name).text.lower() in haystack,
        }
        for p in llm.parties
    ]
    return result
