"""Форматы экспорта. Функции чистые (данные → байты) и выполняются в потоке worker'а.

- PDF — reportlab platypus со встроенным шрифтом DejaVu (кириллица); любой текст проходит через
  `pdf_markup` (экранирование разметки reportlab) — HTML не рендерится.
- DOCX — python-docx, текст пишется как текст (не разметка).
- CSV — UTF-8 с BOM, разделитель `;`; XLSX — openpyxl, все значения — строки (`data_type="s"`).
  В обоих значения проходят через `cell` (защита от formula injection).
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

from docx import Document as Docx
from docx.shared import Pt
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet
from reportlab.lib import colors
from reportlab.lib.fonts import addMapping
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer

from app.exports.content import VALIDATION_SHORT, Report, ReportRisk
from app.exports.safety import cell, clean_text, pdf_markup

FONT_DIR = Path(__file__).with_name("fonts")
FONT = "DejaVuSans"
FONT_BOLD = "DejaVuSans-Bold"
DISCLAIMER_FALLBACK = (
    "Результат подготовлен автоматически с помощью языковой модели и не заменяет консультацию юриста."
)
REVISION_NOTE = "Проект редакции для обсуждения, не готовый юридический текст."
LEGAL_NOTE = "Упоминание норм права сервером не проверено — требует проверки юристом."
REJECTED_NOTE = (
    "Эти риски не подтверждены проверкой цитат и не являются установленным фактом; приведены для полноты."
)

COLUMNS = [
    "ID",
    "Статус проверки",
    "Уровень",
    "Тип",
    "Категория",
    "Затрагивает",
    "Название",
    "Описание",
    "Причина",
    "Последствия",
    "Рекомендация",
    "Проект редакции",
    "Уверенность модели",
    "Цитаты",
    "Страницы",
    "Пункты",
    "Фрагменты",
    "Область поиска",
]


def _fmt_dt(value: object) -> str:
    return value.strftime("%d.%m.%Y %H:%M UTC") if hasattr(value, "strftime") else "—"


def _scope_lines(risk: ReportRisk) -> list[str]:
    scope = risk.search_scope or {}
    lines = []
    if scope.get("model_scope"):
        lines.append(f"Просмотрено моделью: {scope['model_scope']}")
    sections = scope.get("checked_sections") or []
    if sections:
        lines.append("Проверенные разделы: " + "; ".join(str(s) for s in sections))
    lines.extend(f"Ограничение: {x}" for x in scope.get("limitations") or [])
    return lines


def _row(risk: ReportRisk) -> list[str]:
    return [
        risk.key,
        VALIDATION_SHORT.get(risk.validation_status, risk.validation_status),
        risk.severity,
        risk.kind,
        risk.category,
        risk.affected_party,
        risk.title,
        risk.description,
        risk.reason,
        risk.consequences,
        risk.recommendation,
        risk.suggested_revision or "",
        "" if risk.model_confidence is None else f"{risk.model_confidence:.2f}",
        "\n".join(f"«{e.quote}»" for e in risk.evidence),
        "; ".join(e.pages or "—" for e in risk.evidence),
        "; ".join(e.clause_ref or "—" for e in risk.evidence),
        "; ".join(e.fragment_key for e in risk.evidence),
        "\n".join(_scope_lines(risk)),
    ]


# ------------------------------------------------------------------ CSV / XLSX


def render_csv(report: Report) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    for risk in report.all_risks:
        writer.writerow([cell(v) for v in _row(risk)])
    return buffer.getvalue().encode("utf-8-sig")


def render_xlsx(report: Report) -> bytes:

    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Риски"
    _xlsx_rows(ws, [COLUMNS, *(_row(r) for r in report.all_risks)])
    for c in ws[1]:
        c.font = Font(bold=True)
    for index, column in enumerate(ws.columns, start=1):
        letter = get_column_letter(index)
        ws.column_dimensions[letter].width = 18 if index <= 6 else 40
        for c in column:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    info = wb.create_sheet("Отчёт")
    _xlsx_rows(
        info,
        [
            ["Документ", report.document_title],
            ["Версия", str(report.version_no)],
            ["Сторона", report.party + (f": {report.party_name}" if report.party_name else "")],
            ["Глубина", report.depth],
            ["Категории", ", ".join(report.categories)],
            *[[k, v] for k, v in report.model],
            ["Сформирован", _fmt_dt(report.generated_at)],
            ["Итог", report.statement],
            *[["Ограничение", x] for x in report.limitations],
            ["Оговорка", report.disclaimer or DISCLAIMER_FALLBACK],
        ],
    )
    info.column_dimensions["A"].width = 28
    info.column_dimensions["B"].width = 100
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _xlsx_rows(ws: Worksheet, rows: Iterable[list[str]]) -> None:
    for r, values in enumerate(rows, start=1):
        for c, value in enumerate(values, start=1):
            target = ws.cell(row=r, column=c)
            # Только строки: значение не станет формулой даже без префикса.
            target.value = cell(value)
            target.data_type = "s"


# ------------------------------------------------------------------ DOCX


def render_docx(report: Report) -> bytes:

    doc = Docx()
    style = doc.styles["Normal"]
    style.font.name = "Arial"
    style.font.size = Pt(10)

    def para(text: str, *, bold: bool = False, italic: bool = False) -> None:
        run = doc.add_paragraph().add_run(clean_text(text))
        run.bold = bold
        run.italic = italic

    def heading(text: str, level: int) -> None:
        doc.add_heading(clean_text(text), level=level)

    heading("Отчёт об анализе договора", 0)
    para(report.disclaimer or DISCLAIMER_FALLBACK, italic=True)
    for label, value in _meta(report):
        p = doc.add_paragraph()
        p.add_run(clean_text(f"{label}: ")).bold = True
        p.add_run(clean_text(value))
    heading("Итог", 1)
    para(report.statement)
    for label, value in report.counts:
        para(f"{label}: {value}")
    if report.coverage:
        heading("Охват", 1)
        for line in report.coverage:
            para(line)
    heading("Риски", 1)
    if not report.risks:
        para("Подтверждённых или требующих проверки рисков нет в проанализированных разделах.")
    for risk in report.risks:
        _docx_risk(doc, risk, heading, para)
    if report.rejected:
        heading("Отклонённые и не подтверждённые риски", 1)
        para(REJECTED_NOTE, italic=True)
        for risk in report.rejected:
            para(f"{risk.key}. {risk.title} — {risk.validation}")
    heading("Ограничения", 1)
    for line in report.limitations:
        para(f"• {line}")
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _docx_risk(doc: object, risk: ReportRisk, heading: object, para: object) -> None:
    heading(f"{risk.key}. {risk.title}", 2)  # type: ignore[operator]
    para(f"{risk.severity} · {risk.kind} · {risk.category} · затрагивает: {risk.affected_party}")  # type: ignore[operator]
    para(f"Статус проверки: {risk.validation}", bold=True)  # type: ignore[operator]
    for label, value in _risk_fields(risk):
        para(f"{label}: {value}")  # type: ignore[operator]
    for e in risk.evidence:
        place = ", ".join(
            x
            for x in (
                f"стр. {e.pages}" if e.pages else "",
                f"п. {e.clause_ref}" if e.clause_ref else "",
                e.fragment_key,
            )
            if x
        )
        para(f"«{e.quote}»", italic=True)  # type: ignore[operator]
        para(f"{place} — {e.match}; {e.relevance}" + (f" ({e.rationale})" if e.rationale else ""))  # type: ignore[operator]
    for line in _scope_lines(risk):
        para(line)  # type: ignore[operator]


def _risk_fields(risk: ReportRisk) -> list[tuple[str, str]]:
    fields = [
        ("Описание", risk.description),
        ("Причина", risk.reason),
        ("Последствия", risk.consequences),
        ("Рекомендация", risk.recommendation),
    ]
    if risk.suggested_revision:
        fields.append(("Проект редакции", f"{risk.suggested_revision} ({REVISION_NOTE})"))
    if risk.model_confidence is not None:
        fields.append(("Уверенность модели (оценка модели, не вероятность)", f"{risk.model_confidence:.2f}"))
    if risk.legal_reference:
        fields.append(("Внимание", LEGAL_NOTE))
    return fields


def _meta(report: Report) -> list[tuple[str, str]]:
    party = report.party + (f": {report.party_name}" if report.party_name else "")
    meta = [
        ("Документ", report.document_title),
        ("Версия документа", str(report.version_no)),
        ("Анализ", f"{report.analysis_id} · {report.analysis_status}"),
        ("Запущен", _fmt_dt(report.created_at)),
        ("Завершён", _fmt_dt(report.finished_at)),
        ("Сторона анализа", party),
        ("Глубина", report.depth),
        ("Категории", ", ".join(report.categories)),
        *report.model,
        ("Отчёт сформирован", _fmt_dt(report.generated_at)),
    ]
    if report.page_note:
        meta.append(("Страницы", report.page_note))
    return meta


# ------------------------------------------------------------------ PDF


@lru_cache(maxsize=1)
def _register_fonts() -> None:

    pdfmetrics.registerFont(TTFont(FONT, str(FONT_DIR / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont(FONT_BOLD, str(FONT_DIR / "DejaVuSans-Bold.ttf")))
    addMapping(FONT, 0, 0, FONT)
    addMapping(FONT, 1, 0, FONT_BOLD)
    addMapping(FONT, 0, 1, FONT)
    addMapping(FONT, 1, 1, FONT_BOLD)


def render_pdf(report: Report) -> bytes:

    _register_fonts()
    base = ParagraphStyle("base", fontName=FONT, fontSize=9.5, leading=13, splitLongWords=True)
    small = ParagraphStyle(
        "small", parent=base, fontSize=8.5, leading=11, textColor=colors.HexColor("#334155")
    )
    quote = ParagraphStyle(
        "quote",
        parent=base,
        leftIndent=8 * mm,
        textColor=colors.HexColor("#1e293b"),
        backColor=colors.HexColor("#f1f5f9"),
        borderPadding=3,
        spaceBefore=3,
        spaceAfter=3,
    )
    h0 = ParagraphStyle("h0", parent=base, fontName=FONT_BOLD, fontSize=16, leading=20, spaceAfter=6)
    h1 = ParagraphStyle(
        "h1", parent=base, fontName=FONT_BOLD, fontSize=12.5, leading=16, spaceBefore=10, spaceAfter=4
    )
    h2 = ParagraphStyle(
        "h2", parent=base, fontName=FONT_BOLD, fontSize=10.5, leading=14, spaceBefore=8, spaceAfter=2
    )

    def p(text: str, style: ParagraphStyle = base) -> Paragraph:
        return Paragraph(pdf_markup(text), style)

    def labeled(label: str, value: str, style: ParagraphStyle = base) -> Paragraph:
        return Paragraph(f"<b>{pdf_markup(label)}:</b> {pdf_markup(value)}", style)

    story: list[object] = [
        p("Отчёт об анализе договора", h0),
        p(report.disclaimer or DISCLAIMER_FALLBACK, small),
        Spacer(1, 4 * mm),
    ]
    story += [labeled(k, v) for k, v in _meta(report)]
    story += [p("Итог", h1), p(report.statement)]
    story += [labeled(k, v, small) for k, v in report.counts]
    if report.coverage:
        story += [p("Охват", h1), *(p(line, small) for line in report.coverage)]
    story.append(p("Риски", h1))
    if not report.risks:
        story.append(p("Подтверждённых или требующих проверки рисков нет в проанализированных разделах."))
    for risk in report.risks:
        block: list[object] = [
            p(f"{risk.key}. {risk.title}", h2),
            p(f"{risk.severity} · {risk.kind} · {risk.category} · затрагивает: {risk.affected_party}", small),
            labeled("Статус проверки", risk.validation),
        ]
        block += [labeled(k, v) for k, v in _risk_fields(risk)]
        for e in risk.evidence:
            place = ", ".join(
                x
                for x in (
                    f"стр. {e.pages}" if e.pages else "",
                    f"п. {e.clause_ref}" if e.clause_ref else "",
                    e.fragment_key,
                )
                if x
            )
            block.append(p(f"«{e.quote}»", quote))
            block.append(
                p(f"{place} — {e.match}; {e.relevance}" + (f" ({e.rationale})" if e.rationale else ""), small)
            )
        block += [p(line, small) for line in _scope_lines(risk)]
        story.append(KeepTogether(block[:3]))
        story += block[3:]
    if report.rejected:
        story += [p("Отклонённые и не подтверждённые риски", h1), p(REJECTED_NOTE, small)]
        story += [p(f"{r.key}. {r.title} — {r.validation}", small) for r in report.rejected]
    story += [p("Ограничения", h1), *(p(f"• {line}", small) for line in report.limitations)]

    out = io.BytesIO()
    doc = SimpleDocTemplate(
        out,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title="Отчёт об анализе договора",
        author="Contract AI Analyzer",
    )
    doc.build(story)
    return out.getvalue()


RENDERERS = {"pdf": render_pdf, "docx": render_docx, "csv": render_csv, "xlsx": render_xlsx}
MEDIA_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
