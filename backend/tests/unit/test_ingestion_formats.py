"""Приём файла: формат по сигнатуре, расширение, MIME, ZIP/OLE-проверки, имя файла."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.ingestion.formats import (
    FileRejected,
    detect_and_validate,
    extension_of,
    sanitize_filename,
    title_from_filename,
)
from tests import docfactory as f

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _rejected(path: Path, name: str, mime: str | None = None) -> str:
    with pytest.raises(FileRejected) as exc:
        detect_and_validate(path, name, mime)
    return exc.value.code


def test_valid_formats(tmp_path: Path) -> None:
    pdf = f.text_pdf(tmp_path / "a.pdf", [["Текст договора"]])
    docx = f.contract_docx(tmp_path / "a.docx")
    assert detect_and_validate(pdf, "a.pdf", "application/pdf").format == "pdf"
    assert detect_and_validate(docx, "a.docx", DOCX_MIME).format == "docx"
    doc = detect_and_validate(f.FIXTURES / "contract.doc", "contract.doc", "application/msword")
    assert (doc.format, doc.mime_type) == ("doc", "application/msword")


def test_unknown_mime_is_accepted_but_signature_decides(tmp_path: Path) -> None:
    pdf = f.text_pdf(tmp_path / "a.pdf", [["Текст"]])
    for mime in (None, "", "application/octet-stream"):
        assert detect_and_validate(pdf, "a.pdf", mime).mime_type == "application/pdf"


def test_empty_file(tmp_path: Path) -> None:
    assert _rejected(f.write(tmp_path / "e.pdf", b""), "e.pdf") == "empty_file"


@pytest.mark.parametrize(
    "content",
    [
        b"<html><script>alert(1)</script></html>",
        b"MZ\x90\x00 executable",
        b"plain text contract",
        b"\x89PNG\r\n",
    ],
)
def test_unknown_signature(tmp_path: Path, content: bytes) -> None:
    assert (
        _rejected(f.write(tmp_path / "x.pdf", content), "x.pdf", "application/pdf") == "unsupported_file_type"
    )


def test_extension_must_match_content(tmp_path: Path) -> None:
    pdf = f.text_pdf(tmp_path / "a.pdf", [["Текст"]])
    docx = f.contract_docx(tmp_path / "a.docx")
    assert _rejected(pdf, "contract.docx") == "file_type_mismatch"
    assert _rejected(docx, "contract.pdf") == "file_type_mismatch"
    assert _rejected(f.FIXTURES / "contract.doc", "contract.docx") == "file_type_mismatch"
    assert _rejected(pdf, "contract.exe") == "file_type_mismatch"


def test_declared_mime_must_match_content(tmp_path: Path) -> None:
    pdf = f.text_pdf(tmp_path / "a.pdf", [["Текст"]])
    docx = f.contract_docx(tmp_path / "a.docx")
    assert _rejected(pdf, "a.pdf", "text/html") == "mime_type_mismatch"
    assert _rejected(pdf, "a.pdf", DOCX_MIME) == "mime_type_mismatch"
    assert _rejected(docx, "a.docx", "application/pdf") == "mime_type_mismatch"


def test_zip_bomb_rejected(tmp_path: Path) -> None:
    assert _rejected(f.zip_bomb_docx(tmp_path / "b.docx"), "b.docx") == "archive_limits_exceeded"


def test_too_many_zip_entries(tmp_path: Path) -> None:
    entries = {f"word/media/{i}.bin": b"x" for i in range(2001)}
    path = f.raw_docx(tmp_path / "m.docx", f.paragraph_xml("Текст"), extra_entries=entries)
    assert _rejected(path, "m.docx") == "archive_limits_exceeded"


def test_docm_rejected(tmp_path: Path) -> None:
    assert _rejected(f.docm(tmp_path / "m.docx"), "m.docx") == "macros_not_allowed"
    vba_only = f.raw_docx(
        tmp_path / "v.docx", f.paragraph_xml("x"), extra_entries={"word/vbaProject.bin": b"1"}
    )
    assert _rejected(vba_only, "v.docx") == "macros_not_allowed"


def test_path_traversal_in_zip_rejected(tmp_path: Path) -> None:
    assert _rejected(f.traversal_docx(tmp_path / "t.docx"), "t.docx") == "unsafe_archive_path"
    absolute = f.raw_docx(tmp_path / "a.docx", f.paragraph_xml("x"), extra_entries={"/etc/x": b"1"})
    assert _rejected(absolute, "a.docx") == "unsafe_archive_path"


def test_other_zip_formats_rejected(tmp_path: Path) -> None:
    path = tmp_path / "book.docx"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("xl/workbook.xml", "<workbook/>")
    assert _rejected(path, "book.docx") == "unsupported_file_type"


def test_truncated_zip_is_corrupted(tmp_path: Path) -> None:
    data = f.contract_docx(tmp_path / "c.docx").read_bytes()
    assert _rejected(f.write(tmp_path / "t.docx", data[: len(data) // 2]), "t.docx") == "corrupted_file"


def test_non_word_ole_rejected(tmp_path: Path) -> None:
    # OLE-заголовок без потока WordDocument (например, XLS или мусор после сигнатуры).
    fake = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 1024
    assert _rejected(f.write(tmp_path / "x.doc", fake), "x.doc") in {
        "corrupted_file",
        "unsupported_file_type",
    }


def test_sanitize_filename() -> None:
    assert sanitize_filename("../../etc/passwd.pdf") == "passwd.pdf"
    assert sanitize_filename("C:\\Users\\me\\Договор.docx") == "Договор.docx"
    # Управляющие символы и bidi-override (подмена отображаемого расширения) удаляются.
    assert sanitize_filename("evil\u202egpj.pdf") == "evilgpj.pdf"
    assert sanitize_filename("a\x00b\nc.pdf") == "abc.pdf"
    assert sanitize_filename("   .hidden.pdf ") == "hidden.pdf"
    long_name = "д" * 300 + ".docx"
    cleaned = sanitize_filename(long_name)
    assert len(cleaned) <= 200 and cleaned.endswith(".docx")


def test_extension_and_title() -> None:
    assert extension_of("Договор.PDF") == "pdf"
    assert extension_of("noext") == ""
    assert title_from_filename("Договор поставки.docx") == "Договор поставки"
    assert title_from_filename(".pdf") == ".pdf"
