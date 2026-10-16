"""Сборка канонического текста версии, стабильных фрагментов и страниц (ARCHITECTURE §8.5).

- Канонический текст — тексты фрагментов через `\\n\\n`; смещения `char_start/char_end` — индексы
  в нём (в символах Unicode, code points). Текст фрагмента хранится как извлечён: меняются только
  переводы строк (`\\r\\n` → `\\n`) и пробелы по краям.
- Длинные абзацы (> MAX_FRAGMENT_CHARS) делятся по границам предложений; части помечаются в meta.
- Ключ фрагмента `F{seq:06d}` — порядковый номер в версии; фрагменты после сохранения не меняются.
- Страницы: диапазон символов каждой страницы — от начала первого фрагмента (или разрыва внутри
  фрагмента) на этой странице до начала следующей. Если страницы неизвестны — записей нет.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from app.ingestion.model import RawExtraction, RawFragment

SEPARATOR = "\n\n"
MAX_FRAGMENT_CHARS = 2000
_SPLIT_KINDS = {"paragraph", "list_item", "footnote"}
_SENTENCE_END = re.compile(r"[.!?…;:][\"'»”)\]]*\s+")
_WHITESPACE = re.compile(r"\s+")


@dataclass
class FragmentRecord:
    seq: int
    fragment_key: str
    kind: str
    text: str
    text_sha256: str
    char_start: int
    char_end: int
    page_start: int | None
    page_end: int | None
    clause_ref: str | None
    heading_path: list[str]
    table_ref: dict[str, Any] | None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class PageRecord:
    page_no: int
    char_start: int
    char_end: int


@dataclass
class Assembled:
    canonical_text: str
    fragments: list[FragmentRecord]
    pages: list[PageRecord]
    page_count: int | None


def split_spans(text: str, limit: int = MAX_FRAGMENT_CHARS) -> list[tuple[int, int]]:
    """Границы частей длинного текста: по концам предложений, иначе по пробелу, иначе жёстко."""
    if len(text) <= limit:
        return [(0, len(text))]
    spans: list[tuple[int, int]] = []
    start = 0
    while len(text) - start > limit:
        window_end = start + limit
        cut = None
        for m in _SENTENCE_END.finditer(text, start + limit // 4, window_end):
            cut = m.end()
        if cut is None:
            for m in _WHITESPACE.finditer(text, start + limit // 4, window_end):
                cut = m.end()
        if cut is None or cut <= start:
            cut = window_end
        spans.append((start, cut))
        start = cut
    spans.append((start, len(text)))
    # Без пробелов по краям частей.
    result = []
    for s, e in spans:
        piece = text[s:e]
        lead = len(piece) - len(piece.lstrip())
        trail = len(piece) - len(piece.rstrip())
        if e - trail > s + lead:
            result.append((s + lead, e - trail))
    return result


def _page_at(fragment: RawFragment, offset: int) -> int | None:
    page = fragment.page_start
    for off, p in fragment.page_breaks:
        if off <= offset:
            page = p
    return page


def _pieces(fragment: RawFragment) -> list[RawFragment]:
    text = fragment.text.replace("\r\n", "\n").replace("\r", "\n")
    if fragment.kind not in _SPLIT_KINDS or len(text) <= MAX_FRAGMENT_CHARS:
        return [fragment] if text == fragment.text else [_with_text(fragment, text)]
    spans = split_spans(text)
    pieces = []
    for idx, (s, e) in enumerate(spans, start=1):
        meta = dict(fragment.meta)
        meta["split"] = {"part": idx, "of": len(spans)}
        pieces.append(
            RawFragment(
                kind=fragment.kind,
                text=text[s:e],
                page_start=_page_at(fragment, s),
                page_end=_page_at(fragment, e - 1),
                page_breaks=[(off - s, p) for off, p in fragment.page_breaks if s < off < e],
                # Номер пункта относится к началу абзаца — только у первой части.
                clause_ref=fragment.clause_ref if idx == 1 else None,
                heading_path=list(fragment.heading_path),
                table_ref=fragment.table_ref,
                meta=meta,
            )
        )
    return pieces


def _with_text(fragment: RawFragment, text: str) -> RawFragment:
    return RawFragment(
        kind=fragment.kind,
        text=text,
        page_start=fragment.page_start,
        page_end=fragment.page_end,
        page_breaks=fragment.page_breaks,
        clause_ref=fragment.clause_ref,
        heading_path=fragment.heading_path,
        table_ref=fragment.table_ref,
        meta=fragment.meta,
    )


def assemble(raw: RawExtraction) -> Assembled:
    records: list[FragmentRecord] = []
    parts: list[str] = []
    pos = 0
    page_starts: dict[int, int] = {}
    for fragment in raw.fragments:
        for piece in _pieces(fragment):
            text = piece.text.strip()
            if not text:
                continue
            if parts:
                parts.append(SEPARATOR)
                pos += len(SEPARATOR)
            seq = len(records)
            meta = dict(piece.meta)
            if piece.page_breaks:
                meta["page_breaks"] = [[off, p] for off, p in piece.page_breaks]
            records.append(
                FragmentRecord(
                    seq=seq,
                    fragment_key=f"F{seq:06d}",
                    kind=piece.kind,
                    text=text,
                    text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    char_start=pos,
                    char_end=pos + len(text),
                    page_start=piece.page_start,
                    page_end=piece.page_end if piece.page_end is not None else piece.page_start,
                    clause_ref=piece.clause_ref,
                    heading_path=piece.heading_path,
                    table_ref=piece.table_ref,
                    meta=meta,
                )
            )
            # Сноски стоят в конце текста, а не на странице ссылки — границы страниц по ним не считаем.
            if piece.page_start is not None and piece.kind != "footnote":
                page_starts.setdefault(piece.page_start, pos)
                for off, p in piece.page_breaks:
                    page_starts.setdefault(p, pos + off)
            parts.append(text)
            pos += len(text)
    canonical = "".join(parts)

    page_count = raw.page_count
    if page_count is None and page_starts:
        page_count = max(r.page_end or r.page_start or 0 for r in records)
    pages: list[PageRecord] = []
    if raw.page_source is not None and page_count:
        starts: list[int] = []
        for page_no in range(1, page_count + 1):
            starts.append(0 if page_no == 1 else page_starts.get(page_no, -1))
        # Страница без собственного начала (пустая или занята продолжением фрагмента) получает
        # нулевую длину в позиции следующей известной страницы.
        nxt = len(canonical)
        for i in range(page_count - 1, 0, -1):
            if starts[i] < 0:
                starts[i] = nxt
            nxt = starts[i]
        for i in range(1, page_count):
            starts[i] = max(starts[i], starts[i - 1])
        for i, start in enumerate(starts):
            end = starts[i + 1] if i + 1 < page_count else len(canonical)
            while end > start and canonical[end - 1] == "\n":
                end -= 1
            pages.append(PageRecord(page_no=i + 1, char_start=start, char_end=end))
    return Assembled(canonical_text=canonical, fragments=records, pages=pages, page_count=page_count)
