"""Защита экспорта: formula injection, разметка reportlab, недопустимые для XML символы."""

from __future__ import annotations

import pytest

from app.exports.safety import cell, clean_text, pdf_markup


@pytest.mark.parametrize(
    "value",
    ["=1+1", "+1", "-1", "@SUM(A1)", "\tcmd", "\rcmd", "＝1+1", "＋1", "－1", "＠A1", '=HYPERLINK("x")'],
)
def test_formula_like_values_are_prefixed(value: str) -> None:
    assert cell(value).startswith("'")


@pytest.mark.parametrize("value", ["Текст", "1 000 руб.", "0.80", "«=» в середине", "", None, 12])
def test_plain_values_unchanged(value: object) -> None:
    expected = "" if value is None else str(value)
    assert cell(value) == expected


def test_pdf_markup_escapes_tags_and_entities() -> None:
    assert pdf_markup('<a href="x">ссылка</a> & <img src="y"/>') == (
        '&lt;a href="x"&gt;ссылка&lt;/a&gt; &amp; &lt;img src="y"/&gt;'
    )
    assert pdf_markup("строка 1\nстрока 2") == "строка 1<br/>строка 2"


def test_clean_text_removes_xml_illegal_characters() -> None:
    assert clean_text("a\x00b\x07c\x1fd\tе\nж\r\nз") == "abcd\tе\nж\nз"
