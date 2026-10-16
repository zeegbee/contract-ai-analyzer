"""Страницы DOCX по рендеру LibreOffice (`libreoffice_render`, ARCHITECTURE §8.4).

Текст фрагментов ищется в тексте страниц PDF, полученного рендером того же DOCX. Сравнение идёт
только по буквам и цифрам в нижнем регистре: так не мешают переносы строк, автонумерация,
разделители ячеек и типографские знаки. Поиск монотонный (от позиции предыдущего фрагмента).
Не найденный фрагмент остаётся без страницы — страница не угадывается.
"""

from __future__ import annotations

import bisect
import unicodedata

from app.ingestion.model import RawFragment

_PROBE = 40
# Короткий фрагмент («Подпись») ищется только рядом с текущей позицией, чтобы не «прыгнуть» далеко.
_SHORT = 12
_SHORT_WINDOW = 3000


def normalize_for_matching(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKC", text).casefold() if ch.isalnum())


class _Pages:
    def __init__(self, page_texts: list[str]) -> None:
        normalized = [normalize_for_matching(t) for t in page_texts]
        self.text = "".join(normalized)
        self.starts: list[int] = []
        pos = 0
        for item in normalized:
            self.starts.append(pos)
            pos += len(item)

    def page_of(self, pos: int) -> int:
        return bisect.bisect_right(self.starts, pos)  # 1-based


def _inner_breaks(text: str, found: int, key_len: int, pages: _Pages) -> list[tuple[int, int]]:
    """Начала страниц внутри фрагмента: позиция в нормализованном тексте → смещение в исходном тексте.

    Предполагается, что текст фрагмента совпал с рендером подряд (найдено по началу); переходы
    дальше длины фрагмента не учитываются.
    """
    # Индексы символов исходного текста, оставшихся после нормализации (только буквы/цифры).
    originals = [i for i, ch in enumerate(text) if normalize_for_matching(ch)]
    if len(originals) != key_len:
        # NFKC/casefold изменили длину (лигатуры и т.п.) — точное обратное отображение невозможно.
        return []
    breaks = []
    for page_index, start in enumerate(pages.starts):
        offset = start - found
        if 0 < offset < key_len:
            breaks.append((originals[offset], page_index + 1))
    return breaks


def map_fragments_to_pages(fragments: list[RawFragment], page_texts: list[str]) -> int:
    """Проставляет page_start/page_end. Возвращает число фрагментов, для которых страница не найдена."""
    pages = _Pages(page_texts)
    cursor = 0
    unmapped = 0
    for fragment in fragments:
        key = normalize_for_matching(fragment.text)
        if not key:
            continue
        # Сноски в PDF стоят внизу страниц, а у нас — в конце: ищем их от начала, без курсора.
        start_from = 0 if fragment.kind == "footnote" else cursor
        head = key[:_PROBE]
        if len(key) < _SHORT:
            found = pages.text.find(head, start_from, start_from + _SHORT_WINDOW + len(head))
        else:
            found = pages.text.find(head, start_from)
        if found < 0:
            fragment.page_start = fragment.page_end = None
            unmapped += 1
            continue
        end = found + len(head)
        if len(key) > _PROBE:
            tail = key[-_PROBE:]
            tail_at = pages.text.find(tail, found, found + len(key) + 500)
            end = tail_at + len(tail) if tail_at >= 0 else min(found + len(key), len(pages.text))
        fragment.page_start = pages.page_of(found)
        fragment.page_end = pages.page_of(max(end - 1, found))
        fragment.page_breaks = _inner_breaks(fragment.text, found, len(key), pages)
        if fragment.kind != "footnote":
            cursor = end
    return unmapped
