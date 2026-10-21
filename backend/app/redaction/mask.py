"""Таблица соответствий, замаскированный текст и перевод смещений masked ↔ original.

Таблица строится при запуске анализа из подтверждённого предпросмотра (только помеченные сущности и
вхождения). Метки нумеруются по категории в порядке первого вхождения в тексте; даты — в
хронологическом порядке (`⟦ДАТА_1⟧` раньше `⟦ДАТА_2⟧`), чтобы модель знала хотя бы порядок.

Символы `⟦` и `⟧` в тексте договора заменяются на `[` и `]` (длина сохраняется): текст договора не
может подделать метку.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from app.analysis.evidence import FragmentRef
from app.redaction.categories import BY_CODE

OPEN, CLOSE = "⟦", "⟧"
_ESCAPE = str.maketrans({OPEN: "[", CLOSE: "]"})
# Метка в ответе модели: ⟦ЛИЦО_1⟧, а также [ЛИЦО_1] и ЛИЦО_1 (если такая метка есть в таблице).
_LABEL_IN_TEXT = re.compile(r"[⟦\[]?(?<![\wА-Яа-яЁё])([A-ZА-ЯЁ][A-ZА-ЯЁ_]*_\d+)(?![\wА-Яа-яЁё])[⟧\]]?")


def escape_text(text: str) -> str:
    return text.translate(_ESCAPE)


def _label_order_key(entity: Mapping[str, Any], first: tuple[int, int]) -> tuple[Any, ...]:
    key = str(entity.get("key", ""))
    if entity["category"] in ("dates", "birth_date") and key.startswith("date:"):
        return (0, key, first)
    return (1, "", first)


def build_mapping(entities: Sequence[Mapping[str, Any]], fragment_seq: Mapping[str, int]) -> dict[str, Any]:
    """Сущности предпросмотра (с правками пользователя) → таблица соответствий для анализа."""
    chosen: list[tuple[Mapping[str, Any], list[Mapping[str, Any]], tuple[int, int]]] = []
    for entity in entities:
        if not entity.get("masked", True):
            continue
        occurrences = [
            o for o in entity["occurrences"] if o.get("masked", True) and o["fragment"] in fragment_seq
        ]
        if not occurrences:
            continue
        first = min((fragment_seq[o["fragment"]], int(o["start"])) for o in occurrences)
        chosen.append((entity, occurrences, first))
    counters: dict[str, int] = {}
    labels: dict[str, dict[str, Any]] = {}
    replacements: dict[str, list[list[Any]]] = {}
    for entity, occurrences, _first in sorted(chosen, key=lambda c: _label_order_key(c[0], c[2])):
        prefix = BY_CODE[entity["category"]].label if entity["category"] in BY_CODE else "ДАННЫЕ"
        counters[prefix] = counters.get(prefix, 0) + 1
        label = f"{OPEN}{prefix}_{counters[prefix]}{CLOSE}"
        surfaces = sorted({str(o["text"]) for o in occurrences}, key=len, reverse=True)
        labels[label] = {
            "category": entity["category"],
            "canonical": entity.get("canonical") or surfaces[0],
            "entity": entity.get("id"),
            "surfaces": surfaces,
        }
        for o in occurrences:
            replacements.setdefault(o["fragment"], []).append([int(o["start"]), int(o["end"]), label])
    for items in replacements.values():
        items.sort()
    return {"labels": labels, "replacements": replacements}


def mapping_counts(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Счётчики без значений: категория → число значений (меток), всего значений и вхождений."""
    by_category: dict[str, int] = {}
    for info in mapping["labels"].values():
        by_category[info["category"]] = by_category.get(info["category"], 0) + 1
    return {
        "categories": dict(sorted(by_category.items())),
        "values": len(mapping["labels"]),
        "occurrences": sum(len(v) for v in mapping["replacements"].values()),
    }


# ------------------------------------------------------------------ один фрагмент


@dataclass(frozen=True)
class Segment:
    o_start: int
    o_end: int
    m_start: int
    m_end: int
    label: str


@dataclass(frozen=True)
class MaskedFragment:
    original: str
    masked: str
    segments: tuple[Segment, ...]

    def to_original(self, p: int, *, end: bool) -> int:
        """Позиция в замаскированном тексте → в исходном. Середина метки — к границе сущности."""
        shift = 0
        for s in self.segments:
            if p <= s.m_start:
                return p - shift
            if p < s.m_end:
                return s.o_end if end else s.o_start
            shift = s.m_end - s.o_end
        return p - shift

    def to_masked(self, p: int, *, end: bool) -> int:
        shift = 0
        for s in self.segments:
            if p <= s.o_start:
                return p + shift
            if p < s.o_end:
                return s.m_end if end else s.m_start
            shift = s.m_end - s.o_end
        return p + shift


def mask_fragment(text: str, replacements: Sequence[Sequence[Any]]) -> MaskedFragment:
    parts: list[str] = []
    segments: list[Segment] = []
    pos = mpos = 0
    for start, end, label in sorted((int(r[0]), int(r[1]), str(r[2])) for r in replacements):
        if start < pos or end > len(text) or end <= start:
            continue  # пересечение или выход за границы — защита от повреждённой таблицы
        chunk = escape_text(text[pos:start])
        parts.append(chunk)
        mpos += len(chunk)
        segments.append(Segment(start, end, mpos, mpos + len(label), label))
        parts.append(label)
        mpos += len(label)
        pos = end
    parts.append(escape_text(text[pos:]))
    return MaskedFragment(text, "".join(parts), tuple(segments))


# ------------------------------------------------------------------ документ


class MaskView:
    """Замаскированное представление версии документа для промптов и перевод смещений."""

    def __init__(self, fragments: Sequence[FragmentRef], mapping: Mapping[str, Any]) -> None:
        self.labels: dict[str, dict[str, Any]] = dict(mapping["labels"])
        replacements: Mapping[str, Sequence[Sequence[Any]]] = mapping["replacements"]
        self.original = sorted(fragments, key=lambda f: f.seq)
        self.by_key: dict[str, MaskedFragment] = {}
        masked_refs: list[FragmentRef] = []
        mpos = 0
        previous: FragmentRef | None = None
        for f in self.original:
            mf = mask_fragment(f.text, replacements.get(f.key, ()))
            self.by_key[f.key] = mf
            if previous is not None:
                mpos += max(0, f.char_start - previous.char_end)
            masked_refs.append(replace(f, text=mf.masked, char_start=mpos, char_end=mpos + len(mf.masked)))
            mpos += len(mf.masked)
            previous = f
        self.masked = masked_refs
        self._masked_by_key = {f.key: f for f in masked_refs}
        self._original_by_key = {f.key: f for f in self.original}
        # Подстановка в свободный текст: сначала длинные формы.
        pairs = [
            (s, label) for label, info in self.labels.items() for s in (*info["surfaces"], info["canonical"])
        ]
        self._free = sorted({(s, lbl) for s, lbl in pairs if len(s.strip()) >= 2}, key=lambda p: -len(p[0]))

    # -------------------------------------------------------------- смещения (канонический текст)

    def _locate(self, refs: Sequence[FragmentRef], p: int) -> FragmentRef:
        for f in refs:
            if p <= f.char_end:
                return f
        return refs[-1]

    def to_original_range(self, start: int, end: int) -> tuple[int, int]:
        fs = self._locate(self.masked, start)
        fe = self._locate(self.masked, max(start, end))
        o_start = self._original_by_key[fs.key].char_start + self.by_key[fs.key].to_original(
            max(0, start - fs.char_start), end=False
        )
        o_end = self._original_by_key[fe.key].char_start + self.by_key[fe.key].to_original(
            max(0, end - fe.char_start), end=True
        )
        return o_start, max(o_start, o_end)

    def to_masked_range(self, start: int, end: int) -> tuple[int, int]:
        fs = self._locate(self.original, start)
        fe = self._locate(self.original, max(start, end))
        m_start = self._masked_by_key[fs.key].char_start + self.by_key[fs.key].to_masked(
            max(0, start - fs.char_start), end=False
        )
        m_end = self._masked_by_key[fe.key].char_start + self.by_key[fe.key].to_masked(
            max(0, end - fe.char_start), end=True
        )
        return m_start, max(m_start, m_end)

    # -------------------------------------------------------------- свободный текст

    def unmask(self, text: str) -> tuple[str, bool]:
        """Метки → канонические значения. Возвращает (текст, были ли неизвестные метки ⟦…⟧)."""
        unknown = False

        def substitute(m: re.Match[str]) -> str:
            nonlocal unknown
            label = f"{OPEN}{m.group(1)}{CLOSE}"
            if label in self.labels:
                return str(self.labels[label]["canonical"])
            if m.group(0).startswith(OPEN):
                unknown = True
            return m.group(0)

        return _LABEL_IN_TEXT.sub(substitute, text), unknown

    def unmask_obj(self, value: Any) -> Any:
        """Рекурсивная обратная подстановка в строках JSON-подобного объекта."""
        if isinstance(value, str):
            return self.unmask(value)[0]
        if isinstance(value, list):
            return [self.unmask_obj(v) for v in value]
        if isinstance(value, dict):
            return {k: self.unmask_obj(v) for k, v in value.items()}
        return value

    def mask_free_text(self, text: str) -> str:
        """Значения в тексте вне документа (название стороны от пользователя) → метки."""
        result = escape_text(text)
        for surface, label in self._free:
            result = re.sub(
                r"(?<![\wА-Яа-яЁё])" + re.escape(surface) + r"(?![\wА-Яа-яЁё])",
                label,
                result,
                flags=re.IGNORECASE,
            )
        return result
