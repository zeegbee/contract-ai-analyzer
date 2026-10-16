"""Извлечение текста PDF через PyMuPDF (ARCHITECTURE §8.2).

- Зашифрованный PDF (нужен пароль) отклоняется; число страниц ограничено.
- Фрагмент — текстовый блок страницы (обычно абзац) в порядке потока содержимого PDF;
  страница точная (`pdf_native`), в meta — bbox блока.
- Повторяющиеся на многих страницах блоки у верхнего/нижнего края помечаются как колонтитулы
  (`header_footer`), но остаются в тексте.
- Нет извлекаемого текста (скан) → `unsupported_no_text`; OCR не выполняется.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf

from app.ingestion.clauses import clause_ref_from_text, looks_like_heading
from app.ingestion.model import ExtractionFailed, NoTextLayer, RawExtraction, RawFragment, warning

# Страница считается «без текста», если на ней меньше этого числа букв/цифр.
MIN_ALNUM_PER_PAGE = 5
# Документ без текстового слоя: букв/цифр меньше этого порога на весь файл.
MIN_ALNUM_DOCUMENT = 20
_EDGE_RATIO = 0.08
_REPLACEMENT = "�"


@dataclass(frozen=True)
class PdfLimits:
    max_pages: int
    max_fragments: int = 50_000
    max_chars: int = 5_000_000


@dataclass
class _Block:
    page_no: int
    text: str
    bbox: list[float]
    edge_key: str | None  # ключ для поиска колонтитулов (только для блоков у края страницы)


def _configure() -> None:
    # Сообщения MuPDF не печатаются в stdout/stderr (stdout дочернего процесса — канал результата).
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)


def open_pdf(path: Path, max_pages: int) -> pymupdf.Document:
    _configure()
    try:
        doc = pymupdf.open(str(path), filetype="pdf")
    except Exception as exc:  # PyMuPDF бросает разные типы для повреждённых файлов
        raise ExtractionFailed("corrupted_file") from exc
    if doc.needs_pass:
        doc.close()
        raise ExtractionFailed("encrypted_document")
    if doc.page_count == 0:
        doc.close()
        raise ExtractionFailed("corrupted_file")
    if doc.page_count > max_pages:
        doc.close()
        raise ExtractionFailed("too_many_pages")
    return doc


def _alnum_count(text: str) -> int:
    return sum(ch.isalnum() for ch in text)


def _edge_key(text: str) -> str:
    # Номера страниц и даты в колонтитулах меняются — сравниваем без цифр.
    return re.sub(r"\d+", "#", " ".join(text.split()).lower())


def _read_blocks(doc: pymupdf.Document, limits: PdfLimits) -> tuple[list[_Block], list[int], bool]:
    blocks: list[_Block] = []
    pages_without_text: list[int] = []
    total_chars = 0
    for index in range(doc.page_count):
        page = doc[index]
        page_no = index + 1
        height = float(page.rect.height) or 1.0
        try:
            raw_blocks = page.get_text("blocks", sort=False)
        except Exception as exc:
            raise ExtractionFailed("corrupted_file") from exc
        page_alnum = 0
        for x0, y0, x1, y1, raw_text, _block_no, block_type in raw_blocks:
            if block_type != 0:  # изображение
                continue
            text = str(raw_text).replace("\r\n", "\n").replace("\r", "\n").strip()
            if not text:
                continue
            page_alnum += _alnum_count(text)
            total_chars += len(text)
            if len(blocks) >= limits.max_fragments or total_chars > limits.max_chars:
                raise ExtractionFailed("document_too_large")
            at_edge = y1 <= height * _EDGE_RATIO or y0 >= height * (1 - _EDGE_RATIO)
            blocks.append(
                _Block(
                    page_no=page_no,
                    text=text,
                    bbox=[round(float(v), 1) for v in (x0, y0, x1, y1)],
                    edge_key=_edge_key(text) if at_edge else None,
                )
            )
        if page_alnum < MIN_ALNUM_PER_PAGE:
            pages_without_text.append(page_no)
    return blocks, pages_without_text, bool(getattr(doc, "is_repaired", False))


def _repeated_edge_keys(blocks: list[_Block], page_count: int) -> set[str]:
    if page_count < 3:
        return set()
    pages_by_key: dict[str, set[int]] = {}
    for block in blocks:
        if block.edge_key is not None:
            pages_by_key.setdefault(block.edge_key, set()).add(block.page_no)
    threshold = max(3, page_count // 2)
    return {key for key, pages in pages_by_key.items() if len(pages) >= threshold}


def extract_pdf(path: Path, limits: PdfLimits) -> RawExtraction:
    doc = open_pdf(path, limits.max_pages)
    try:
        page_count = doc.page_count
        blocks, pages_without_text, repaired = _read_blocks(doc, limits)
    finally:
        doc.close()

    if sum(_alnum_count(b.text) for b in blocks) < MIN_ALNUM_DOCUMENT:
        raise NoTextLayer("no_text_layer")

    repeated = _repeated_edge_keys(blocks, page_count)
    fragments: list[RawFragment] = []
    heading_path: list[str] = []
    for block in blocks:
        meta: dict[str, Any] = {"bbox": block.bbox}
        if block.edge_key is not None and block.edge_key in repeated:
            kind = "header_footer"
            meta["repeated"] = True
            clause = None
        else:
            clause = clause_ref_from_text(block.text)
            if clause:
                meta["clause_source"] = "text"
            kind = "paragraph"
            if looks_like_heading(block.text):
                kind = "heading"
                meta["heading_source"] = "text_pattern"
                heading_path = [block.text.split("\n", 1)[0][:200]]
        fragments.append(
            RawFragment(
                kind=kind,
                text=block.text,
                page_start=block.page_no,
                page_end=block.page_no,
                clause_ref=clause,
                heading_path=list(heading_path),
                meta=meta,
            )
        )

    total_chars = sum(len(b.text) for b in blocks)
    replacement_chars = sum(b.text.count(_REPLACEMENT) for b in blocks)
    warnings: list[dict[str, Any]] = []
    if pages_without_text:
        warnings.append(
            warning("pages_without_text", count=len(pages_without_text), pages=pages_without_text[:50])
        )
    if total_chars and replacement_chars / total_chars > 0.02:
        warnings.append(warning("unreadable_characters", share=round(replacement_chars / total_chars, 3)))
    if repaired:
        warnings.append(warning("pdf_repaired"))
    if repeated:
        warnings.append(warning("headers_footers_marked"))
    return RawExtraction(
        fragments=fragments,
        page_count=page_count,
        page_source="pdf_native",
        warnings=warnings,
        needs_page_render=False,
    )


def pdf_page_texts(path: Path, max_pages: int) -> list[str]:
    """Текст каждой страницы (для сопоставления DOCX со страницами рендера LibreOffice)."""
    doc = open_pdf(path, max_pages)
    try:
        return [str(doc[i].get_text("text")) for i in range(doc.page_count)]
    except Exception as exc:
        raise ExtractionFailed("corrupted_file") from exc
    finally:
        doc.close()
