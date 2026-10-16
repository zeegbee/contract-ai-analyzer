"""Определение формата загруженного файла и проверки до разбора (ARCHITECTURE §8.1).

Формат определяется по содержимому (сигнатура + структура контейнера), а не по имени или
заявленному Content-Type. Расширение и Content-Type лишь должны соответствовать найденному формату.
Здесь ничего не распаковывается и не разбирается «по-настоящему»: читаются только заголовки,
центральный каталог ZIP, `[Content_Types].xml` (с ограничением размера) и каталог OLE.
"""

from __future__ import annotations

import posixpath
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import olefile
from lxml import etree

from app.ingestion.model import warning

PDF = "pdf"
DOCX = "docx"
DOC = "doc"

MIME_BY_FORMAT = {
    PDF: "application/pdf",
    DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    DOC: "application/msword",
}
EXTENSIONS = {"pdf": PDF, "docx": DOCX, "doc": DOC}

# Заявленный Content-Type должен быть одним из этих (для найденного формата) или «неизвестным».
_DECLARED_MIME = {
    PDF: {"application/pdf", "application/x-pdf"},
    DOCX: {MIME_BY_FORMAT[DOCX], "application/zip", "application/x-zip-compressed"},
    DOC: {"application/msword", "application/vnd.ms-word"},
}
# Браузер не знает тип файла — сигнатура всё равно проверяется.
_UNKNOWN_MIME = {"", "application/octet-stream", "binary/octet-stream", "application/x-download"}

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGIC = b"PK\x03\x04"
_PDF_MAGIC = b"%PDF-"

DOCX_MAIN_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
_MACRO_TYPES = ("macroenabled", "vbaproject", "ms-office.vba")


@dataclass(frozen=True)
class ZipLimits:
    max_entries: int = 2000
    max_total_uncompressed: int = 200 * 1024 * 1024
    max_entry_uncompressed: int = 100 * 1024 * 1024
    max_ratio: int = 100
    # Коэффициент сжатия проверяется для записей больше этого размера (маленький XML жмётся сильно).
    ratio_min_size: int = 1024 * 1024
    max_content_types_size: int = 1024 * 1024


class FileRejected(Exception):
    """Файл отклонён при приёме. `code` — код ошибки API, сообщение формирует API-слой."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class DetectedFile:
    format: str
    mime_type: str
    warnings: list[dict[str, Any]] = field(default_factory=list)


def safe_xml_parser() -> etree.XMLParser:
    """Парсер для недоверенного XML: без внешних сущностей, DTD, сети и «огромных» деревьев."""
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        remove_comments=True,
        remove_pis=True,
    )


def parse_xml(data: bytes) -> etree._Element:
    # Word не пишет DOCTYPE; его наличие — признак попытки XXE / «billion laughs».
    if b"<!DOCTYPE" in data[:4096] or b"<!ENTITY" in data[:65536]:
        raise FileRejected("corrupted_file")
    try:
        return etree.fromstring(data, parser=safe_xml_parser())
    except etree.XMLSyntaxError as exc:
        raise FileRejected("corrupted_file") from exc


# ---------------------------------------------------------------- имя файла


def sanitize_filename(raw: str, max_length: int = 200) -> str:
    """Имя только для отображения: без пути, управляющих и bidi-символов, ограниченной длины."""
    name = unicodedata.normalize("NFC", raw).replace("\\", "/").rsplit("/", 1)[-1]
    # Категория C* — управляющие, format (в т.ч. U+202E RTL override), неназначенные.
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C")
    name = re.sub(r"\s+", " ", name).strip().lstrip(".").strip()
    if len(name) > max_length:
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 8:
            name = stem[: max_length - len(ext) - 1].rstrip() + "." + ext
        else:
            name = name[:max_length]
    return name


def extension_of(filename: str) -> str:
    _, dot, ext = filename.rpartition(".")
    return ext.lower() if dot else ""


def title_from_filename(filename: str) -> str:
    stem, dot, _ext = filename.rpartition(".")
    title = stem if dot and stem else filename
    return title.strip()[:300] or "Документ"


# ---------------------------------------------------------------- определение формата


def _sniff(path: Path) -> str | None:
    with path.open("rb") as f:
        head = f.read(1024)
    if head.startswith(_OLE_MAGIC):
        return DOC
    if head.startswith(_ZIP_MAGIC):
        return DOCX
    # Стандарт допускает мусор перед заголовком PDF; ищем в первом килобайте.
    if _PDF_MAGIC in head:
        return PDF
    return None


def _safe_member_name(name: str) -> bool:
    if (
        not name
        or "\x00" in name
        or "\\" in name
        or name.startswith("/")
        or ":" in name.split("/", maxsplit=1)[0]
    ):
        return False
    parts = name.split("/")
    return ".." not in parts and posixpath.normpath(name) == name.rstrip("/")


def inspect_docx_zip(path: Path, limits: ZipLimits | None = None) -> list[dict[str, Any]]:
    """Проверки ZIP-контейнера DOCX до разбора: zip-бомба, пути, макросы, тип основной части."""
    limits = limits or ZipLimits()
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise FileRejected("corrupted_file") from exc
    with zf:
        infos = zf.infolist()
        if len(infos) > limits.max_entries:
            raise FileRejected("archive_limits_exceeded")
        names: set[str] = set()
        total = 0
        compressed = 0
        for info in infos:
            if not _safe_member_name(info.filename):
                raise FileRejected("unsafe_archive_path")
            if info.filename in names:
                raise FileRejected("corrupted_file")
            names.add(info.filename)
            if info.flag_bits & 0x1:
                raise FileRejected("encrypted_document")
            if info.file_size > limits.max_entry_uncompressed:
                raise FileRejected("archive_limits_exceeded")
            if info.file_size >= limits.ratio_min_size and info.file_size > limits.max_ratio * max(
                info.compress_size, 1
            ):
                raise FileRejected("archive_limits_exceeded")
            total += info.file_size
            compressed += info.compress_size
        if total > limits.max_total_uncompressed:
            raise FileRejected("archive_limits_exceeded")
        if total >= limits.ratio_min_size and total > limits.max_ratio * max(compressed, 1):
            raise FileRejected("archive_limits_exceeded")
        if any(n.lower().endswith("vbaproject.bin") for n in names):
            raise FileRejected("macros_not_allowed")
        if "[Content_Types].xml" not in names or "word/document.xml" not in names:
            raise FileRejected("unsupported_file_type")
        ct_info = zf.getinfo("[Content_Types].xml")
        if ct_info.file_size > limits.max_content_types_size:
            raise FileRejected("archive_limits_exceeded")
        try:
            content_types = parse_xml(zf.read(ct_info))
        except (zipfile.BadZipFile, OSError, ValueError, EOFError) as exc:
            raise FileRejected("corrupted_file") from exc
    main_type = None
    for el in content_types:
        ctype = (el.get("ContentType") or "").lower()
        if any(marker in ctype for marker in _MACRO_TYPES):
            raise FileRejected("macros_not_allowed")
        if (el.get("PartName") or "").lower() == "/word/document.xml":
            main_type = ctype
    if main_type != DOCX_MAIN_TYPE:
        # Шаблон (.dotx), другой формат OOXML или повреждённый пакет.
        raise FileRejected("unsupported_file_type")
    return []


def inspect_doc_ole(path: Path) -> list[dict[str, Any]]:
    """DOC (Word 97–2003): OLE-контейнер с потоком WordDocument, без шифрования."""
    try:
        ole = olefile.OleFileIO(str(path))
    except (OSError, ValueError) as exc:
        raise FileRejected("corrupted_file") from exc
    with ole:
        if ole.exists("EncryptedPackage") or ole.exists("EncryptionInfo"):
            # Зашифрованный OOXML внутри OLE-контейнера.
            raise FileRejected("encrypted_document")
        if not ole.exists("WordDocument"):
            # Другой OLE-документ (XLS, PPT, MSG…).
            raise FileRejected("unsupported_file_type")
        try:
            fib = ole.openstream("WordDocument").read(12)
        except OSError as exc:
            raise FileRejected("corrupted_file") from exc
        if len(fib) < 12 or fib[0:2] != b"\xec\xa5":
            raise FileRejected("corrupted_file")
        flags = int.from_bytes(fib[10:12], "little")
        if flags & 0x0100:  # FIB.fEncrypted
            raise FileRejected("encrypted_document")
        has_macros = ole.exists("Macros") or ole.exists("_VBA_PROJECT_CUR")
    # Макросы не выполняются: LibreOffice запускается с запретом макросов (converter).
    return [warning("doc_macros_ignored")] if has_macros else []


def detect_and_validate(path: Path, filename: str, declared_mime: str | None) -> DetectedFile:
    """Проверка загруженного файла. Бросает FileRejected с кодом причины."""
    if path.stat().st_size == 0:
        raise FileRejected("empty_file")
    fmt = _sniff(path)
    if fmt is None:
        raise FileRejected("unsupported_file_type")
    if fmt == DOCX:
        warnings = inspect_docx_zip(path)
    elif fmt == DOC:
        warnings = inspect_doc_ole(path)
    else:
        warnings = []
    ext_format = EXTENSIONS.get(extension_of(filename))
    if ext_format != fmt:
        raise FileRejected("file_type_mismatch")
    mime = (declared_mime or "").split(";", 1)[0].strip().lower()
    if mime not in _UNKNOWN_MIME and mime not in _DECLARED_MIME[fmt]:
        raise FileRejected("mime_type_mismatch")
    return DetectedFile(format=fmt, mime_type=MIME_BY_FORMAT[fmt], warnings=warnings)
