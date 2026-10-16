"""Промежуточное представление результата извлечения (передаётся из дочернего процесса как JSON)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


class ExtractionFailed(Exception):
    """Файл нельзя обработать. Повтор бессмыслен: результат не изменится."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class NoTextLayer(ExtractionFailed):
    """В документе нет извлекаемого текста (например, скан без OCR)."""

    def __init__(self, code: str = "no_text_layer") -> None:
        super().__init__(code)


@dataclass
class RawFragment:
    """Фрагмент до сборки канонического текста.

    page_start/page_end — только если страница достоверно известна. page_breaks — позиции внутри
    `text`, с которых начинается новая страница (разрыв страницы посреди абзаца DOCX): (offset, page_no).
    """

    kind: str
    text: str
    page_start: int | None = None
    page_end: int | None = None
    page_breaks: list[tuple[int, int]] = field(default_factory=list)
    clause_ref: str | None = None
    heading_path: list[str] = field(default_factory=list)
    table_ref: dict[str, Any] | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class RawExtraction:
    fragments: list[RawFragment]
    page_count: int | None
    # pdf_native / docx_rendered_breaks / None — страницы ещё не определены
    page_source: str | None
    warnings: list[dict[str, Any]] = field(default_factory=list)
    # DOCX без надёжной разметки страниц Word: нужен рендер в PDF через converter.
    needs_page_render: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RawExtraction:
        fragments = []
        for raw in data["fragments"]:
            item = dict(raw)
            item["page_breaks"] = [(int(o), int(p)) for o, p in item.get("page_breaks", [])]
            fragments.append(RawFragment(**item))
        return cls(
            fragments=fragments,
            page_count=data.get("page_count"),
            page_source=data.get("page_source"),
            warnings=list(data.get("warnings", [])),
            needs_page_render=bool(data.get("needs_page_render", False)),
        )


def warning(code: str, **params: Any) -> dict[str, Any]:
    """Предупреждение извлечения: код (переводится в UI) и параметры без текста документа."""
    return {"code": code, **params}
