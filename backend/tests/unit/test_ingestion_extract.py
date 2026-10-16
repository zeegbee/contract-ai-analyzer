"""Извлечение DOCX/PDF, сборка фрагментов и страниц, пункты, сопоставление со страницами рендера."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ingestion.assemble import MAX_FRAGMENT_CHARS, assemble, split_spans
from app.ingestion.clauses import clause_ref_from_text, looks_like_heading
from app.ingestion.docx import DocxLimits, extract_docx
from app.ingestion.model import ExtractionFailed, NoTextLayer, RawExtraction, RawFragment
from app.ingestion.pagemap import map_fragments_to_pages
from app.ingestion.pdf import PdfLimits, extract_pdf
from tests import docfactory as f

DOCX_LIMITS = DocxLimits(max_pages=500)
PDF_LIMITS = PdfLimits(max_pages=500)


def _texts(raw: RawExtraction) -> list[str]:
    return [fr.text for fr in raw.fragments]


# ---------------------------------------------------------------- DOCX


def test_docx_paragraphs_and_tables_in_document_order(tmp_path: Path) -> None:
    raw = extract_docx(f.contract_docx(tmp_path / "c.docx"), DOCX_LIMITS)
    texts = _texts(raw)
    i_par = texts.index(f.CONTRACT_PARAGRAPHS[1])
    i_head = texts.index("№ | Товар | Цена, руб.")
    i_after = texts.index("Текст после таблицы.")
    assert i_par < i_head < i_after
    rows = [fr for fr in raw.fragments if fr.kind == "table_row"]
    assert [r.table_ref for r in rows] == [{"table": 1, "row": n, "columns": 3} for n in (1, 2, 3)]
    assert rows[1].text == "1 | Бумага А4 | 350"
    assert rows[1].meta["cells"] == ["1", "Бумага А4", "350"]
    # Номер в первой ячейке таблицы не считается пунктом договора.
    assert all(r.clause_ref is None for r in rows)


def test_docx_headings_clauses_and_autonumbering(tmp_path: Path) -> None:
    raw = extract_docx(f.contract_docx(tmp_path / "c.docx"), DOCX_LIMITS)
    by_text = {fr.text: fr for fr in raw.fragments}
    title = by_text["ДОГОВОР ПОСТАВКИ № 15"]
    assert (title.kind, title.meta["heading_source"]) == ("heading", "style")
    clause = by_text[f.CONTRACT_PARAGRAPHS[0]]
    assert (clause.clause_ref, clause.meta["clause_source"]) == ("1.1", "text")
    assert clause.heading_path == ["ДОГОВОР ПОСТАВКИ № 15", "1. Предмет договора"]
    assert by_text[f.CONTRACT_PARAGRAPHS[2]].heading_path[-1] == "2. Цена"
    first = by_text["Первый пункт автонумерации"]
    second = by_text["Второй пункт автонумерации"]
    assert (first.kind, first.meta["list_label"], first.clause_ref) == ("list_item", "1.", "1")
    assert (second.meta["list_label"], second.clause_ref, second.meta["clause_source"]) == (
        "2.",
        "2",
        "autonumber",
    )
    # Обычный абзац без номера — без пункта (ничего не выдумывается).
    assert by_text["Текст после таблицы."].clause_ref is None


def test_docx_multilevel_numbering_and_start_override(tmp_path: Path) -> None:
    numbering = (
        '<w:abstractNum w:abstractNumId="0">'
        '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>'
        '<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2."/></w:lvl>'
        '<w:lvl w:ilvl="2"><w:start w:val="1"/><w:numFmt w:val="russianLower"/>'
        '<w:lvlText w:val="%3)"/></w:lvl>'
        "</w:abstractNum>"
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
    )

    def item(text: str, ilvl: int) -> str:
        ppr = f'<w:pPr><w:numPr><w:ilvl w:val="{ilvl}"/><w:numId w:val="1"/></w:numPr></w:pPr>'
        return f.paragraph_xml(text, ppr=ppr)

    body = (
        item("Предмет", 0)
        + item("Поставка товара", 1)
        + item("Приёмка товара", 1)
        + item("подпункт с буквой", 2)
        + item("Цена", 0)
        + item("Оплата", 1)
    )
    raw = extract_docx(f.raw_docx(tmp_path / "n.docx", body, numbering_xml=numbering), DOCX_LIMITS)
    got = [(fr.meta.get("list_label"), fr.clause_ref) for fr in raw.fragments]
    assert got == [("1.", "1"), ("1.1.", "1.1"), ("1.2.", "1.2"), ("а)", None), ("2.", "2"), ("2.1.", "2.1")]


def test_docx_rendered_page_breaks(tmp_path: Path) -> None:
    raw = extract_docx(f.rendered_pages_docx(tmp_path / "r.docx", app_pages=3), DOCX_LIMITS)
    assert (raw.page_source, raw.page_count, raw.needs_page_render) == ("docx_rendered_breaks", 3, False)
    pages = [(fr.page_start, fr.page_end) for fr in raw.fragments]
    assert pages == [(1, 1), (1, 2), (3, 3)]
    assembled = assemble(raw)
    split = assembled.fragments[1]
    break_at = split.meta["page_breaks"][0][0]
    assert split.text[break_at:].startswith("конец на второй")
    page2 = assembled.pages[1]
    assert assembled.canonical_text[page2.char_start : page2.char_end] == "конец на второй."


def test_docx_breaks_not_trusted_without_matching_page_count(tmp_path: Path) -> None:
    for app_pages in (None, 7):
        raw = extract_docx(
            f.rendered_pages_docx(tmp_path / f"r{app_pages}.docx", app_pages=app_pages), DOCX_LIMITS
        )
        assert raw.page_source is None and raw.needs_page_render
        assert all(fr.page_start is None for fr in raw.fragments)


def test_docx_without_word_layout_needs_render(tmp_path: Path) -> None:
    raw = extract_docx(f.page_break_docx(tmp_path / "p.docx"), DOCX_LIMITS)
    assert raw.needs_page_render and raw.page_count is None


def test_docx_empty_has_no_text(tmp_path: Path) -> None:
    with pytest.raises(NoTextLayer):
        extract_docx(f.empty_docx(tmp_path / "e.docx"), DOCX_LIMITS)


def test_docx_tracked_changes_footnotes_textboxes(tmp_path: Path) -> None:
    body = (
        '<w:p><w:r><w:t xml:space="preserve">Срок поставки </w:t></w:r>'
        '<w:del w:id="1"><w:r><w:delText>10</w:delText></w:r></w:del>'
        '<w:ins w:id="2"><w:r><w:t>20</w:t></w:r></w:ins>'
        '<w:r><w:t xml:space="preserve"> дней.</w:t></w:r>'
        '<w:r><w:footnoteReference w:id="1"/></w:r></w:p>'
        "<w:p><w:r><w:t>Видимый текст</w:t></w:r><w:r><w:drawing><w:txbxContent><w:p><w:r>"
        "<w:t>Текст в надписи</w:t></w:r></w:p></w:txbxContent></w:drawing></w:r></w:p>"
    )
    path = f.raw_docx(tmp_path / "t.docx", body)
    raw = extract_docx(path, DOCX_LIMITS)
    assert _texts(raw) == ["Срок поставки 20 дней.", "Видимый текст"]
    codes = {w["code"] for w in raw.warnings}
    assert {"tracked_changes_applied", "textboxes_skipped"} <= codes
    assert raw.fragments[0].meta["note_refs"] == ["footnote:1"]


def test_docx_nested_table_and_content_controls(tmp_path: Path) -> None:
    inner = "<w:tbl><w:tr><w:tc>" + f.paragraph_xml("Вложенная ячейка") + "</w:tc></w:tr></w:tbl>"
    body = (
        "<w:sdt><w:sdtContent>" + f.paragraph_xml("Абзац в элементе управления") + "</w:sdtContent></w:sdt>"
        "<w:tbl><w:tr><w:tc>" + f.paragraph_xml("Внешняя") + inner + "</w:tc><w:tc>"
        + f.paragraph_xml("Вторая") + "</w:tc></w:tr></w:tbl>"
    )  # fmt: skip
    raw = extract_docx(f.raw_docx(tmp_path / "n.docx", body), DOCX_LIMITS)
    assert _texts(raw) == ["Абзац в элементе управления", "Внешняя | Вторая", "Вложенная ячейка"]
    assert raw.fragments[2].table_ref == {
        "table": 2,
        "row": 1,
        "columns": 1,
        "parent": {"table": 1, "row": 1},
    }


def test_docx_xxe_is_not_resolved(tmp_path: Path) -> None:
    with pytest.raises(ExtractionFailed) as exc:
        extract_docx(f.xxe_docx(tmp_path / "x.docx"), DOCX_LIMITS)
    assert exc.value.code == "corrupted_file"


def test_docx_too_many_pages_by_app_properties(tmp_path: Path) -> None:
    path = f.raw_docx(tmp_path / "p.docx", f.paragraph_xml("Текст"), app_pages=900)
    with pytest.raises(ExtractionFailed) as exc:
        extract_docx(path, DocxLimits(max_pages=500))
    assert exc.value.code == "too_many_pages"


def test_docx_zip_bomb_rejected_before_parsing(tmp_path: Path) -> None:
    with pytest.raises(ExtractionFailed) as exc:
        extract_docx(f.zip_bomb_docx(tmp_path / "b.docx"), DOCX_LIMITS)
    assert exc.value.code == "archive_limits_exceeded"


# ---------------------------------------------------------------- PDF


def test_pdf_pages_and_order(tmp_path: Path) -> None:
    pages = [f.CONTRACT_PARAGRAPHS[:2], ["Статья 2. Цена", f.CONTRACT_PARAGRAPHS[2]]]
    raw = extract_pdf(f.text_pdf(tmp_path / "t.pdf", pages), PDF_LIMITS)
    assert (raw.page_source, raw.page_count) == ("pdf_native", 2)
    flat = [" ".join(fr.text.split()) for fr in raw.fragments]
    assert flat == [*pages[0], *pages[1]]
    assert [fr.page_start for fr in raw.fragments] == [1, 1, 2, 2]
    assert [fr.clause_ref for fr in raw.fragments] == ["1.1", "1.2", "Статья 2", "2.1"]
    assert raw.fragments[2].kind == "heading"
    assert raw.fragments[3].heading_path == ["Статья 2. Цена"]
    assert len(raw.fragments[0].meta["bbox"]) == 4


def test_pdf_headers_footers_marked_but_kept(tmp_path: Path) -> None:
    raw = extract_pdf(f.header_footer_pdf(tmp_path / "h.pdf", pages=4), PDF_LIMITS)
    kinds = [fr.kind for fr in raw.fragments]
    assert kinds.count("header_footer") == 8 and kinds.count("paragraph") == 4
    assert {"code": "headers_footers_marked"} in raw.warnings


def test_pdf_without_text_layer(tmp_path: Path) -> None:
    with pytest.raises(NoTextLayer) as exc:
        extract_pdf(f.scanned_pdf(tmp_path / "s.pdf"), PDF_LIMITS)
    assert exc.value.code == "no_text_layer"


def test_pdf_partial_scan_warns_about_pages(tmp_path: Path) -> None:
    import pymupdf

    path = f.text_pdf(tmp_path / "p.pdf", [["Первая страница с текстом договора"], ["Третья страница"]])
    doc = pymupdf.open(str(path))
    doc.new_page(pno=1)  # пустая страница посередине
    doc.save(str(tmp_path / "p2.pdf"))
    doc.close()
    raw = extract_pdf(tmp_path / "p2.pdf", PDF_LIMITS)
    assert {"code": "pages_without_text", "count": 1, "pages": [2]} in raw.warnings


def test_pdf_encrypted_corrupted_and_page_limit(tmp_path: Path) -> None:
    cases = {
        "encrypted_document": (f.encrypted_pdf(tmp_path / "e.pdf"), PDF_LIMITS),
        "corrupted_file": (f.write(tmp_path / "c.pdf", b"%PDF-1.7\n1 0 obj garbage"), PDF_LIMITS),
        "too_many_pages": (f.many_pages_pdf(tmp_path / "m.pdf", 6), PdfLimits(max_pages=5)),
    }
    for code, (path, limits) in cases.items():
        with pytest.raises(ExtractionFailed) as exc:
            extract_pdf(path, limits)
        assert exc.value.code == code


# ---------------------------------------------------------------- сборка и страницы


def test_assemble_offsets_and_stable_keys(tmp_path: Path) -> None:
    raw = extract_docx(f.contract_docx(tmp_path / "c.docx"), DOCX_LIMITS)
    assembled = assemble(raw)
    for i, fr in enumerate(assembled.fragments):
        assert fr.seq == i and fr.fragment_key == f"F{i:06d}"
        assert assembled.canonical_text[fr.char_start : fr.char_end] == fr.text
    assert assembled.canonical_text == "\n\n".join(fr.text for fr in assembled.fragments)
    # Повторное извлечение того же файла даёт те же фрагменты.
    again = assemble(extract_docx(tmp_path / "c.docx", DOCX_LIMITS))
    assert [(x.fragment_key, x.text_sha256) for x in again.fragments] == [
        (x.fragment_key, x.text_sha256) for x in assembled.fragments
    ]


def test_long_paragraph_split_by_sentences(tmp_path: Path) -> None:
    raw = extract_docx(f.long_paragraph_docx(tmp_path / "l.docx", sentences=120), DOCX_LIMITS)
    assembled = assemble(raw)
    parts = assembled.fragments
    assert len(parts) > 1
    assert all(len(p.text) <= MAX_FRAGMENT_CHARS for p in parts)
    assert all(p.text.endswith(".") for p in parts)  # граница — конец предложения
    assert [p.meta["split"]["part"] for p in parts] == list(range(1, len(parts) + 1))
    joined = " ".join(p.text for p in parts)
    assert joined == raw.fragments[0].text


def test_split_spans_without_sentence_boundaries() -> None:
    text = "а" * 4500
    spans = split_spans(text, 2000)
    assert [e - s for s, e in spans] == [2000, 2000, 500]


def test_pdf_page_ranges(tmp_path: Path) -> None:
    raw = extract_pdf(f.text_pdf(tmp_path / "t.pdf", [["Первая страница"], ["Вторая страница"]]), PDF_LIMITS)
    assembled = assemble(raw)
    assert [(p.page_no, assembled.canonical_text[p.char_start : p.char_end]) for p in assembled.pages] == [
        (1, "Первая страница"),
        (2, "Вторая страница"),
    ]


def test_map_fragments_to_rendered_pages() -> None:
    fragments = [
        RawFragment(kind="paragraph", text="1.1. Поставщик обязуется передать товар Покупателю."),
        RawFragment(kind="table_row", text="Бумага А4 | 350"),
        RawFragment(kind="paragraph", text="Текст, которого нет в рендере вообще никак."),
        RawFragment(kind="paragraph", text="Абзац, начатый на второй странице и законченный на третьей."),
        RawFragment(kind="footnote", text="Сноска к пункту первому."),
    ]
    page_texts = [
        "Договор\n1.1. Поставщик обязуется\nпередать товар Покупателю.\nБумага А4\n350\n"
        "Сноска к пункту первому.",
        "Абзац, начатый на второй стра-\nнице и",
        "законченный на третьей.",
    ]
    unmapped = map_fragments_to_pages(fragments, page_texts)
    assert unmapped == 1
    assert [(fr.page_start, fr.page_end) for fr in fragments] == [
        (1, 1),
        (1, 1),
        (None, None),
        (2, 3),
        (1, 1),
    ]
    spanning = fragments[3]
    [(offset, page)] = spanning.page_breaks
    assert page == 3 and spanning.text[offset:].startswith("законченный")


def test_rendered_pages_survive_paragraph_split() -> None:
    """Регрессия: длинный абзац на нескольких страницах рендера после деления на части."""
    sentences = [f"Предложение {i} описывает обязанность стороны." for i in range(150)]
    text = " ".join(sentences)
    third = len(sentences) // 3
    page_texts = [
        " ".join(sentences[:third]),
        " ".join(sentences[third : 2 * third]),
        " ".join(sentences[2 * third :]),
    ]
    raw = RawExtraction(
        fragments=[RawFragment(kind="paragraph", text=text)], page_count=None, page_source=None
    )
    assert map_fragments_to_pages(raw.fragments, page_texts) == 0
    raw.page_source, raw.page_count = "libreoffice_render", 3
    assembled = assemble(raw)
    assert len(assembled.fragments) > 1
    assert assembled.fragments[0].page_start == 1 and assembled.fragments[-1].page_end == 3
    assert {p for f in assembled.fragments for p in (f.page_start, f.page_end)} == {1, 2, 3}
    starts = [p.char_start for p in assembled.pages]
    assert starts == sorted(starts) and len(set(starts)) == 3
    second = assembled.pages[1]
    assert assembled.canonical_text[second.char_start :].startswith(sentences[third])


# ---------------------------------------------------------------- пункты


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4.2. Покупатель обязан", "4.2"),
        ("4.2 Покупатель обязан", "4.2"),
        ("10.3.1. Подпункт", "10.3.1"),
        ("5. ПРЕДМЕТ ДОГОВОРА", "5"),
        ("Статья 7. Ответственность", "Статья 7"),
        ("РАЗДЕЛ III. Цена", "Раздел III"),
        ("п. 3.4 договора применяется", "3.4"),
        ("01.02.2026 г. стороны подписали", None),
        ("1.500 руб. оплачивается", None),
        ("5 дней с момента", None),
        ("Покупатель обязан по п. 4.2", None),
    ],
)
def test_clause_ref_only_when_explicit(text: str, expected: str | None) -> None:
    assert clause_ref_from_text(text) == expected


def test_heading_heuristics() -> None:
    assert looks_like_heading("Статья 5. Цена договора")
    assert looks_like_heading("5. ПРЕДМЕТ ДОГОВОРА")
    assert looks_like_heading("ОТВЕТСТВЕННОСТЬ СТОРОН")
    assert not looks_like_heading("ИНН 7701234567")
    assert not looks_like_heading("5. Поставщик обязан поставить товар.")
    assert not looks_like_heading(
        "Статья 5 настоящего договора применяется к отношениям сторон, возникшим ранее,"
    )
