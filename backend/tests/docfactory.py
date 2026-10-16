"""Генерация тестовых документов (синтетические договоры, без реальных данных).

DOCX — python-docx и «ручной» OOXML (для разметки страниц Word, макросов, zip-бомб);
PDF — PyMuPDF (кириллица через insert_htmlbox).
"""

from __future__ import annotations

import html
import io
import zipfile
from pathlib import Path

import docx
import pymupdf
from docx.enum.text import WD_BREAK

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

CONTRACT_PARAGRAPHS = [
    "1.1. Поставщик обязуется передать Покупателю товар в собственность в сроки, установленные договором.",
    "1.2. Покупатель обязуется принять товар и оплатить его в течение 30 (тридцати) календарных дней.",
    "2.1. Цена товара указывается в спецификации и не подлежит изменению в одностороннем порядке.",
]


# ---------------------------------------------------------------- DOCX (python-docx)


def contract_docx(path: Path) -> Path:
    """Заголовки, нумерованные абзацы, таблица между абзацами, автонумерация списка."""
    d = docx.Document()
    d.add_heading("ДОГОВОР ПОСТАВКИ № 15", level=1)
    d.add_paragraph("г. Москва")
    d.add_heading("1. Предмет договора", level=2)
    for text in CONTRACT_PARAGRAPHS[:2]:
        d.add_paragraph(text)
    table = d.add_table(rows=3, cols=3)
    for r, row in enumerate(
        [("№", "Товар", "Цена, руб."), ("1", "Бумага А4", "350"), ("2", "Тонер", "4 200")]
    ):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    d.add_paragraph("Текст после таблицы.")
    d.add_heading("2. Цена", level=2)
    d.add_paragraph(CONTRACT_PARAGRAPHS[2])
    d.add_paragraph("Первый пункт автонумерации", style="List Number")
    d.add_paragraph("Второй пункт автонумерации", style="List Number")
    d.save(str(path))
    return path


def empty_docx(path: Path) -> Path:
    d = docx.Document()
    d.add_paragraph("")
    d.save(str(path))
    return path


def page_break_docx(path: Path) -> Path:
    d = docx.Document()
    d.add_paragraph("Первая страница договора.")
    d.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
    d.add_paragraph("Вторая страница договора.")
    d.save(str(path))
    return path


def long_paragraph_docx(path: Path, sentences: int = 120) -> Path:
    d = docx.Document()
    d.add_paragraph(
        " ".join(f"Предложение номер {i} описывает обязанность стороны." for i in range(sentences))
    )
    d.save(str(path))
    return path


# ---------------------------------------------------------------- DOCX (ручной OOXML)

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="{main}"/>'
    "{extra}"
    "</Types>"
)
DOCX_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
DOCM_MAIN = "application/vnd.ms-word.document.macroEnabled.main+xml"
_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    'officeDocument" Target="word/document.xml"/></Relationships>'
)


def paragraph_xml(text: str, *, before: str = "", ppr: str = "") -> str:
    return f'<w:p>{ppr}<w:r>{before}<w:t xml:space="preserve">{html.escape(text)}</w:t></w:r></w:p>'


def raw_docx(
    path: Path,
    body_xml: str,
    *,
    main_type: str = DOCX_MAIN,
    app_pages: int | None = None,
    extra_entries: dict[str, bytes] | None = None,
    numbering_xml: str | None = None,
) -> Path:
    files: dict[str, bytes] = {
        "_rels/.rels": _ROOT_RELS.encode(),
        "word/document.xml": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="{W_NS}">'
            f"<w:body>{body_xml}</w:body></w:document>"
        ).encode(),
    }
    extra_types = ""
    rels = []
    if numbering_xml is not None:
        files["word/numbering.xml"] = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<w:numbering xmlns:w="{W_NS}">{numbering_xml}</w:numbering>'
        ).encode()
        rels.append(
            '<Relationship Id="rId9" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/numbering" Target="numbering.xml"/>'
        )
    if rels:
        files["word/_rels/document.xml.rels"] = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(rels)
            + "</Relationships>"
        ).encode()
    if app_pages is not None:
        files["docProps/app.xml"] = (
            '<?xml version="1.0" encoding="UTF-8"?><Properties xmlns="http://schemas.openxmlformats.org/'
            f'officeDocument/2006/extended-properties"><Pages>{app_pages}</Pages></Properties>'
        ).encode()
    files["[Content_Types].xml"] = _CONTENT_TYPES.format(main=main_type, extra=extra_types).encode()
    files.update(extra_entries or {})
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def rendered_pages_docx(path: Path, *, app_pages: int | None = 3) -> Path:
    """Разметка страниц, сохранённая Word: lastRenderedPageBreak в начале и посреди абзаца."""
    lrpb = "<w:lastRenderedPageBreak/>"
    body = (
        paragraph_xml("Абзац на первой странице.")
        + f'<w:p><w:r><w:t xml:space="preserve">Начало на первой, </w:t></w:r><w:r>{lrpb}'
        '<w:t xml:space="preserve">конец на второй.</w:t></w:r></w:p>'
        + paragraph_xml("Абзац на третьей странице.", before=lrpb)
    )
    return raw_docx(path, body, app_pages=app_pages)


def zip_bomb_docx(path: Path) -> Path:
    # 5 МБ нулей сжимаются примерно в 5 КБ: коэффициент ~1000 (порог — 100).
    return raw_docx(
        path, paragraph_xml("Текст"), extra_entries={"word/media/bomb.bin": b"\0" * 5 * 1024 * 1024}
    )


def docm(path: Path) -> Path:
    return raw_docx(
        path,
        paragraph_xml("Документ с макросами"),
        main_type=DOCM_MAIN,
        extra_entries={"word/vbaProject.bin": b"\xd0\xcf\x11\xe0fake-vba"},
    )


def traversal_docx(path: Path) -> Path:
    return raw_docx(path, paragraph_xml("Текст"), extra_entries={"../../evil.txt": b"x"})


def xxe_docx(path: Path) -> Path:
    body = (
        '<?xml version="1.0"?><!DOCTYPE w:document [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        f'<w:document xmlns:w="{W_NS}"><w:body><w:p><w:r><w:t>&xxe;</w:t></w:r></w:p></w:body></w:document>'
    )
    raw_docx(path, "")
    buf = io.BytesIO(path.read_bytes())
    with zipfile.ZipFile(buf) as src, zipfile.ZipFile(path, "w") as dst:
        for info in src.infolist():
            data = body.encode() if info.filename == "word/document.xml" else src.read(info)
            dst.writestr(info.filename, data)
    return path


# ---------------------------------------------------------------- PDF


def text_pdf(path: Path, pages: list[list[str]]) -> Path:
    doc = pymupdf.open()
    for paragraphs in pages:
        page = doc.new_page()
        html_body = "".join(f"<p>{html.escape(p)}</p>" for p in paragraphs)
        page.insert_htmlbox(pymupdf.Rect(60, 80, 540, 760), html_body)
    doc.save(str(path))
    doc.close()
    return path


def scanned_pdf(path: Path, pages: int = 2) -> Path:
    """«Скан»: только изображение, без текстового слоя."""
    doc = pymupdf.open()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 100), False)
    pix.clear_with(200)
    for _ in range(pages):
        doc.new_page().insert_image(pymupdf.Rect(50, 50, 550, 300), pixmap=pix)
    doc.save(str(path))
    doc.close()
    return path


def encrypted_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Secret contract text")
    doc.save(
        str(path),
        encryption=pymupdf.PDF_ENCRYPT_AES_256,  # type: ignore[attr-defined]
        owner_pw="owner",
        user_pw="user",
    )
    doc.close()
    return path


def many_pages_pdf(path: Path, pages: int) -> Path:
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page(width=200, height=200).insert_text((20, 50), f"Page {i + 1} contract terms")
    doc.save(str(path))
    doc.close()
    return path


def header_footer_pdf(path: Path, pages: int = 4) -> Path:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_htmlbox(pymupdf.Rect(60, 15, 540, 45), "<p>ООО Ромашка — договор поставки</p>")
        page.insert_htmlbox(
            pymupdf.Rect(60, 200, 540, 400), f"<p>Содержательный пункт договора номер {i + 1}.</p>"
        )
        page.insert_htmlbox(pymupdf.Rect(60, 800, 540, 830), f"<p>Страница {i + 1} из {pages}</p>")
    doc.save(str(path))
    doc.close()
    return path


def write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path
