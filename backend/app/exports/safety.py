"""Безопасная запись недоверенного текста в файлы экспорта."""

from __future__ import annotations

import re
from xml.sax.saxutils import escape

# Символы, недопустимые в XML 1.0 (DOCX, XLSX, разметка reportlab): управляющие, кроме \t \n \r.
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
# Первые символы, с которых табличные редакторы начинают формулу (включая полноширинные варианты).
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "\uff1d", "\uff0b", "\uff0d", "\uff20")


def clean_text(value: str) -> str:
    """Убирает управляющие символы, недопустимые в XML; переводы строк сохраняются."""
    return _XML_ILLEGAL.sub("", value.replace("\r\n", "\n"))


def pdf_markup(value: str) -> str:
    """Текст для `reportlab.platypus.Paragraph`: разметка (`<`, `>`, `&`) экранируется,
    поэтому текст договора или ответа модели не может добавить теги, ссылки или изображения."""
    return escape(clean_text(value)).replace("\n", "<br/>")


def cell(value: object) -> str:
    """Значение ячейки CSV/XLSX: строка, которая не будет интерпретирована как формула."""
    text = "" if value is None else clean_text(str(value))
    if text.startswith(_FORMULA_START):
        return "'" + text
    return text
