"""Извлечение текста DOCX (ARCHITECTURE §8.2).

Пакет читается напрямую (zipfile + lxml с безопасным парсером), а не через python-docx: так под
контролем и настройки XML-парсера (без сущностей, DTD и сети), и набор читаемых частей —
только document.xml, styles, numbering, сноски и docProps/app.xml.

Порядок: обход `w:body` — абзацы и таблицы в порядке документа; content controls (`w:sdt`)
разворачиваются; таблица → по фрагменту на строку (ячейки через « | »), вложенные таблицы — после
строки-родителя; сноски — в конце. Удалённый текст исправлений (`w:del`, `w:moveFrom`), коды полей и
надписи (text box) не включаются — об этом пишется предупреждение.

Страницы: если Word сохранил разметку (`w:lastRenderedPageBreak`) и число разрывов согласуется с
`docProps/app.xml` (Pages), страницы берутся из неё (`docx_rendered_breaks`). Иначе извлечение
возвращает `needs_page_render=True`, и страницы определяются рендером LibreOffice (pagemap).
"""

from __future__ import annotations

import posixpath
import re
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lxml import etree

from app.ingestion.clauses import clause_ref_from_label, clause_ref_from_text, looks_like_heading
from app.ingestion.docx_numbering import Numbering, w, wval
from app.ingestion.formats import FileRejected, inspect_docx_zip, parse_xml
from app.ingestion.model import ExtractionFailed, NoTextLayer, RawExtraction, RawFragment, warning

MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
EP_NS = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"

W_P, W_TBL, W_TR, W_TC, W_SDT, W_SDT_CONTENT, W_CUSTOM_XML, W_SMART_TAG = (
    w("p"), w("tbl"), w("tr"), w("tc"), w("sdt"), w("sdtContent"), w("customXml"), w("smartTag"),
)  # fmt: skip
W_T, W_TAB, W_BR, W_CR, W_LRPB, W_NBH = (
    w("t"),
    w("tab"),
    w("br"),
    w("cr"),
    w("lastRenderedPageBreak"),
    w("noBreakHyphen"),
)
W_DEL, W_MOVE_FROM, W_INS, W_MOVE_TO = w("del"), w("moveFrom"), w("ins"), w("moveTo")
W_DEL_TEXT, W_INSTR, W_DEL_INSTR = w("delText"), w("instrText"), w("delInstrText")
W_TXBX, W_PPR, W_RPR = w("txbxContent"), w("pPr"), w("rPr")
W_FOOTNOTE_REF, W_ENDNOTE_REF = w("footnoteReference"), w("endnoteReference")
MC_FALLBACK = f"{{{MC_NS}}}Fallback"

# Подтеги, которые не содержат текста документа или содержат удалённый/служебный текст.
_SKIP = {W_PPR, W_RPR, W_DEL, W_MOVE_FROM, W_DEL_TEXT, W_INSTR, W_DEL_INSTR, MC_FALLBACK, w("sectPr")}
_UNWRAP = {W_SDT, W_CUSTOM_XML, W_SMART_TAG}

_HEADING_STYLE_RE = re.compile(r"^(?:heading|заголовок)\s*([1-9])$", re.IGNORECASE)
_TITLE_STYLES = {"title", "название", "заголовок"}


@dataclass(frozen=True)
class DocxLimits:
    max_pages: int
    max_part_size: int = 60 * 1024 * 1024
    max_fragments: int = 50_000
    max_chars: int = 5_000_000


@dataclass
class _Style:
    name: str
    based_on: str | None
    outline_level: int | None
    num: tuple[str, int] | None


class _Styles:
    def __init__(self, root: etree._Element | None) -> None:
        self.by_id: dict[str, _Style] = {}
        self.default_paragraph: str | None = None
        if root is None:
            return
        for st in root.iter(w("style")):
            sid = wval(st, "styleId")
            if sid is None:
                continue
            ppr = st.find(W_PPR)
            num = None
            outline = None
            if ppr is not None:
                num = _num_pr(ppr.find(w("numPr")))
                outline_val = wval(ppr.find(w("outlineLvl")))
                outline = int(outline_val) if outline_val and outline_val.isdigit() else None
            self.by_id[sid] = _Style(
                name=(wval(st.find(w("name"))) or sid)[:100],
                based_on=wval(st.find(w("basedOn"))),
                outline_level=outline,
                num=num,
            )
            if wval(st, "type") == "paragraph" and wval(st, "default") in ("1", "true"):
                self.default_paragraph = sid

    def _chain(self, style_id: str | None) -> list[_Style]:
        chain: list[_Style] = []
        seen: set[str] = set()
        current = style_id or self.default_paragraph
        while current and current not in seen and len(chain) < 20:
            seen.add(current)
            style = self.by_id.get(current)
            if style is None:
                break
            chain.append(style)
            current = style.based_on
        return chain

    def name(self, style_id: str | None) -> str | None:
        chain = self._chain(style_id)
        return chain[0].name if chain else None

    def heading_level(self, style_id: str | None) -> int | None:
        for style in self._chain(style_id):
            lowered = style.name.strip().lower()
            if m := _HEADING_STYLE_RE.match(lowered):
                return int(m.group(1))
            if lowered in _TITLE_STYLES:
                return 1
            if style.outline_level is not None and style.outline_level < 9:
                return style.outline_level + 1
        return None

    def numbering(self, style_id: str | None) -> tuple[str, int] | None:
        for style in self._chain(style_id):
            if style.num is not None:
                return style.num
        return None


def _num_pr(el: etree._Element | None) -> tuple[str, int] | None:
    if el is None:
        return None
    num_id = wval(el.find(w("numId")))
    ilvl = wval(el.find(w("ilvl")))
    if num_id is None:
        return None
    return num_id, int(ilvl) if ilvl and ilvl.isdigit() else 0


class _Parts:
    def __init__(self, zf: zipfile.ZipFile, max_part_size: int) -> None:
        self.zf = zf
        self.names = set(zf.namelist())
        self.max = max_part_size

    def read(self, name: str) -> bytes | None:
        if name not in self.names:
            return None
        info = self.zf.getinfo(name)
        if info.file_size > self.max:
            raise ExtractionFailed("archive_limits_exceeded")
        try:
            with self.zf.open(info) as f:
                data = f.read(self.max + 1)
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, OSError) as exc:
            raise ExtractionFailed("corrupted_file") from exc
        if len(data) > self.max:
            raise ExtractionFailed("archive_limits_exceeded")
        return data

    def xml(self, name: str | None) -> etree._Element | None:
        if name is None:
            return None
        data = self.read(name)
        if data is None:
            return None
        try:
            return parse_xml(data)
        except FileRejected as exc:
            raise ExtractionFailed(exc.code) from exc

    def relationships(self, rels_name: str, base_dir: str) -> dict[str, list[str]]:
        """Тип связи (последний сегмент URI) → имена частей внутри пакета."""
        root = self.xml(rels_name)
        result: dict[str, list[str]] = {}
        if root is None:
            return result
        for rel in root.iter(f"{{{REL_NS}}}Relationship"):
            if rel.get("TargetMode") == "External":
                continue
            target = rel.get("Target") or ""
            name = target.lstrip("/") if target.startswith("/") else posixpath.join(base_dir, target)
            name = posixpath.normpath(name)
            if name.startswith("..") or name not in self.names:
                continue
            rel_type = (rel.get("Type") or "").rsplit("/", 1)[-1]
            result.setdefault(rel_type, []).append(name)
        return result


@dataclass
class _ParaText:
    text: str
    events: list[int]  # смещения w:lastRenderedPageBreak в тексте
    note_refs: list[tuple[str, str]] = field(default_factory=list)
    textboxes: int = 0
    tracked: bool = False


def _read_runs(p: etree._Element) -> _ParaText:
    parts: list[str] = []
    length = 0
    result = _ParaText(text="", events=[])

    def add(s: str) -> None:
        nonlocal length
        parts.append(s)
        length += len(s)

    def visit(el: etree._Element) -> None:
        for child in el:
            tag = child.tag
            if not isinstance(tag, str):
                continue
            if tag in _SKIP:
                if tag in (W_DEL, W_MOVE_FROM):
                    result.tracked = True
                continue
            if tag == W_T:
                add(child.text or "")
            elif tag == W_TAB:
                add("\t")
            elif tag == W_BR:
                # Разрыв страницы (`type=page`) — не текст; страницы берутся из lastRenderedPageBreak.
                if wval(child, "type") not in ("page", "column"):
                    add("\n")
            elif tag == W_CR:
                add("\n")
            elif tag == W_NBH:
                add("‑")
            elif tag == W_LRPB:
                result.events.append(length)
            elif tag in (W_FOOTNOTE_REF, W_ENDNOTE_REF):
                kind = "footnote" if tag == W_FOOTNOTE_REF else "endnote"
                result.note_refs.append((kind, wval(child, "id") or ""))
            elif tag == W_TXBX:
                result.textboxes += 1
            else:
                if tag in (W_INS, W_MOVE_TO):
                    result.tracked = True
                visit(child)

    visit(p)
    result.text = "".join(parts)
    return result


def _strip_with_events(text: str, events: list[int]) -> tuple[str, list[int]]:
    """Обрезка пробелов по краям с пересчётом смещений разрывов страниц."""
    lead = len(text) - len(text.lstrip())
    stripped = text.strip()
    return stripped, [min(max(e - lead, 0), len(stripped)) for e in events]


def _unwrapped(el: etree._Element) -> list[etree._Element]:
    """Дочерние элементы с развёрнутыми w:sdt / w:customXml / w:smartTag."""
    out: list[etree._Element] = []
    for child in el:
        if not isinstance(child.tag, str):
            continue
        if child.tag == W_SDT:
            content = child.find(W_SDT_CONTENT)
            if content is not None:
                out.extend(_unwrapped(content))
        elif child.tag in _UNWRAP:
            out.extend(_unwrapped(child))
        else:
            out.append(child)
    return out


@dataclass
class _ParaInfo:
    text: str
    events: list[int]
    label: str | None
    label_numeric: bool
    style: str | None
    heading_level: int | None
    note_refs: list[tuple[str, str]]


class _Walker:
    def __init__(self, styles: _Styles, numbering: Numbering, rendered: bool, limits: DocxLimits) -> None:
        self.styles = styles
        self.numbering = numbering
        self.rendered = rendered
        self.limits = limits
        self.fragments: list[RawFragment] = []
        self.heading_stack: list[tuple[int, str]] = []
        self.page = 1
        self.tables = 0
        self.chars = 0
        self.textboxes = 0
        self.tracked = False
        self.note_pages: dict[tuple[str, str], int] = {}

    # ---------------------------------------------------------------- общие

    def _add(self, fragment: RawFragment) -> None:
        self.chars += len(fragment.text)
        if len(self.fragments) >= self.limits.max_fragments or self.chars > self.limits.max_chars:
            raise ExtractionFailed("document_too_large")
        self.fragments.append(fragment)

    def _heading_path(self) -> list[str]:
        return [text for _, text in self.heading_stack]

    def _read_paragraph(self, p: etree._Element) -> _ParaInfo:
        runs = _read_runs(p)
        self.textboxes += runs.textboxes
        self.tracked = self.tracked or runs.tracked
        ppr = p.find(W_PPR)
        style_id = wval(ppr.find(w("pStyle"))) if ppr is not None else None
        num = _num_pr(ppr.find(w("numPr"))) if ppr is not None else None
        if num is None:
            num = self.styles.numbering(style_id)
        label = None
        numeric = False
        if num is not None and num[0] != "0":
            computed = self.numbering.next_label(num[0], num[1])
            if computed is not None and computed.label:
                label, numeric = computed.label, computed.numeric
        heading_level = self.styles.heading_level(style_id)
        if ppr is not None and heading_level is None:
            outline = wval(ppr.find(w("outlineLvl")))
            if outline and outline.isdigit() and int(outline) < 9:
                heading_level = int(outline) + 1
        text, events = _strip_with_events(runs.text, runs.events)
        return _ParaInfo(
            text=text,
            events=events,
            label=label,
            label_numeric=numeric,
            style=self.styles.name(style_id),
            heading_level=heading_level,
            note_refs=runs.note_refs,
        )

    def _advance_pages(self, text_len: int, events: list[int]) -> tuple[int, int, list[tuple[int, int]]]:
        """Применяет разрывы страниц абзаца. Возвращает (page_start, page_end, внутренние разрывы)."""
        events = sorted(events)
        for e in events:
            if e == 0:
                self.page += 1
        start = self.page
        breaks: list[tuple[int, int]] = []
        for e in events:
            if 0 < e < text_len:
                self.page += 1
                breaks.append((e, self.page))
        end = self.page
        self.page += sum(1 for e in events if e >= text_len > 0)
        return start, end, breaks

    def _record_note_refs(self, refs: list[tuple[str, str]], page: int | None) -> None:
        if page is None:
            return
        for ref in refs:
            self.note_pages.setdefault(ref, page)

    # ---------------------------------------------------------------- обход

    def walk(self, container: etree._Element) -> None:
        for el in _unwrapped(container):
            if el.tag == W_P:
                self._paragraph(el)
            elif el.tag == W_TBL:
                self._table(el, parent=None)

    def _paragraph(self, p: etree._Element) -> None:
        info = self._read_paragraph(p)
        if not info.text:
            # Пустой абзац: разрывы страниц внутри него всё равно учитываются.
            self.page += len(info.events)
            return
        page_start, page_end, breaks = self._advance_pages(len(info.text), info.events)
        meta: dict[str, Any] = {}
        if info.style:
            meta["style"] = info.style
        if info.label:
            meta["list_label"] = info.label
        clause = None
        if info.label and info.label_numeric:
            clause = clause_ref_from_label(info.label)
            if clause:
                meta["clause_source"] = "autonumber"
        if clause is None and not info.label:
            clause = clause_ref_from_text(info.text)
            if clause:
                meta["clause_source"] = "text"
        level = info.heading_level
        if level is not None:
            meta["heading_source"] = "style"
        elif looks_like_heading(info.text):
            level = 1
            meta["heading_source"] = "text_pattern"
        if level is not None:
            kind = "heading"
            meta["heading_level"] = level
            while self.heading_stack and self.heading_stack[-1][0] >= level:
                self.heading_stack.pop()
            self.heading_stack.append((level, info.text.split("\n", 1)[0][:200]))
        else:
            kind = "list_item" if info.label else "paragraph"
        if info.note_refs:
            meta["note_refs"] = [f"{k}:{i}" for k, i in info.note_refs]
            self._record_note_refs(info.note_refs, page_start if self.rendered else None)
        self._add(
            RawFragment(
                kind=kind,
                text=info.text,
                page_start=page_start if self.rendered else None,
                page_end=page_end if self.rendered else None,
                page_breaks=breaks if self.rendered else [],
                clause_ref=clause,
                heading_path=self._heading_path(),
                meta=meta,
            )
        )

    def _table(self, tbl: etree._Element, parent: dict[str, int] | None) -> None:
        self.tables += 1
        table_no = self.tables
        rows = [el for el in _unwrapped(tbl) if el.tag == W_TR]
        for row_no, tr in enumerate(rows, start=1):
            cells: list[str] = []
            nested: list[etree._Element] = []
            page_start: int | None = None
            note_refs: list[tuple[str, str]] = []
            for tc in (el for el in _unwrapped(tr) if el.tag == W_TC):
                texts: list[str] = []
                for item in _unwrapped(tc):
                    if item.tag == W_P:
                        info = self._read_paragraph(item)
                        if info.text:
                            if page_start is None:
                                for e in info.events:
                                    if e == 0:
                                        self.page += 1
                                page_start = self.page
                                self.page += sum(1 for e in info.events if e > 0)
                            else:
                                self.page += len(info.events)
                            texts.append(info.text)
                            note_refs.extend(info.note_refs)
                        else:
                            self.page += len(info.events)
                    elif item.tag == W_TBL:
                        nested.append(item)
                cells.append("\n".join(texts))
            if any(cells):
                meta: dict[str, Any] = {"cells": cells}
                trpr = tr.find(w("trPr"))
                if trpr is not None and trpr.find(w("tblHeader")) is not None:
                    meta["header_row"] = True
                if note_refs:
                    meta["note_refs"] = [f"{k}:{i}" for k, i in note_refs]
                    self._record_note_refs(note_refs, page_start if self.rendered else None)
                table_ref: dict[str, Any] = {"table": table_no, "row": row_no, "columns": len(cells)}
                if parent:
                    table_ref["parent"] = parent
                self._add(
                    RawFragment(
                        kind="table_row",
                        text=" | ".join(cells),
                        page_start=page_start if self.rendered else None,
                        page_end=self.page if self.rendered else None,
                        heading_path=self._heading_path(),
                        table_ref=table_ref,
                        meta=meta,
                    )
                )
            for inner in nested:
                self._table(inner, parent={"table": table_no, "row": row_no})

    def notes(self, root: etree._Element | None, kind: str) -> int:
        if root is None:
            return 0
        count = 0
        for note in root.iter(w(kind)):
            if wval(note, "type") in ("separator", "continuationSeparator", "continuationNotice"):
                continue
            note_id = wval(note, "id") or ""
            texts = [self._read_paragraph(p).text for p in note.iter(W_P)]
            text = "\n".join(t for t in texts if t)
            if not text:
                continue
            page = self.note_pages.get((kind, note_id)) if self.rendered else None
            self._add(
                RawFragment(
                    kind="footnote",
                    text=text,
                    page_start=page,
                    page_end=page,
                    meta={"note_type": kind, "note_id": note_id},
                )
            )
            count += 1
        return count


def _app_pages(parts: _Parts) -> int | None:
    root = parts.xml("docProps/app.xml") if "docProps/app.xml" in parts.names else None
    if root is None:
        return None
    el = root.find(f"{{{EP_NS}}}Pages")
    if el is None or not (el.text or "").strip().isdigit():
        return None
    pages = int((el.text or "0").strip())
    return pages if pages > 0 else None


def _has_text(root: etree._Element | None) -> bool:
    return root is not None and any((t.text or "").strip() for t in root.iter(W_T))


def extract_docx(path: Path, limits: DocxLimits) -> RawExtraction:
    try:
        inspect_docx_zip(path)
    except FileRejected as exc:
        raise ExtractionFailed(exc.code) from exc
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ExtractionFailed("corrupted_file") from exc
    with zf:
        parts = _Parts(zf, limits.max_part_size)
        document = parts.xml("word/document.xml")
        rels = parts.relationships("word/_rels/document.xml.rels", "word")

        def first(rel_type: str) -> str | None:
            names = rels.get(rel_type)
            return names[0] if names else None

        styles = _Styles(parts.xml(first("styles")))
        numbering = Numbering(parts.xml(first("numbering")))
        footnotes = parts.xml(first("footnotes"))
        endnotes = parts.xml(first("endnotes"))
        headers_with_text = any(
            _has_text(parts.xml(name)) for rel in ("header", "footer") for name in rels.get(rel, [])
        )
        app_pages = _app_pages(parts)

    body = document.find(w("body")) if document is not None else None
    if body is None:
        raise ExtractionFailed("corrupted_file")
    if app_pages is not None and app_pages > limits.max_pages:
        raise ExtractionFailed("too_many_pages")

    rendered_breaks = sum(1 for _ in body.iter(W_LRPB))
    # Разметке Word доверяем, только если она согласуется с числом страниц, которое Word записал сам.
    rendered = rendered_breaks > 0 and app_pages is not None and rendered_breaks + 1 == app_pages

    walker = _Walker(styles, numbering, rendered, limits)
    walker.walk(body)
    walker.notes(footnotes, "footnote")
    walker.notes(endnotes, "endnote")

    if not any(f.text.strip() for f in walker.fragments):
        raise NoTextLayer("no_text")

    warnings: list[dict[str, Any]] = []
    if walker.tracked:
        warnings.append(warning("tracked_changes_applied"))
    if walker.textboxes:
        warnings.append(warning("textboxes_skipped", count=walker.textboxes))
    if headers_with_text:
        warnings.append(warning("headers_footers_skipped"))
    return RawExtraction(
        fragments=walker.fragments,
        page_count=app_pages if rendered else None,
        page_source="docx_rendered_breaks" if rendered else None,
        warnings=warnings,
        needs_page_render=not rendered,
    )
