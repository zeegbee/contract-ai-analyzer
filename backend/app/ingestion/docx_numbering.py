"""Вычисление автонумерации DOCX (`numbering.xml`) для типовых случаев.

Поддерживается: форматы decimal/decimalZero/буквы/римские/русские буквы/маркеры, шаблон `lvlText`
(«%1.%2.»), `start`, `startOverride`, перезапуск нижних уровней при росте верхнего.
Не поддерживается (номер не вычисляется — `None`): `numStyleLink`/`styleLink`, `isLgl`,
`lvlRestart`, нестандартные форматы. Номер пункта (`clause_ref`) берётся из метки только если все
уровни в шаблоне числовые — иначе он не считается достоверным.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def wval(el: etree._Element | None, attr: str = "val") -> str | None:
    if el is None:
        return None
    value = el.get(w(attr))
    return value if value is None else str(value)


_NUMERIC_FORMATS = {"decimal", "decimalZero"}
_RU_LOWER = "абвгдежзиклмнопрстуфхцчшщэюя"


@dataclass
class Level:
    fmt: str
    text: str
    start: int


@dataclass
class AbstractNum:
    levels: dict[int, Level] = field(default_factory=dict)
    supported: bool = True


@dataclass
class NumInstance:
    abstract_id: str
    start_overrides: dict[int, int] = field(default_factory=dict)


@dataclass
class ListLabel:
    label: str
    numeric: bool


def _to_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _roman(n: int) -> str:
    pairs = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"),
             (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]  # fmt: skip
    out = []
    for value, sym in pairs:
        while n >= value:
            out.append(sym)
            n -= value
    return "".join(out)


def _letters(n: int, alphabet: str) -> str:
    # Word: a…z, затем aa…zz, aaa… (повтор буквы).
    if n <= 0:
        return ""
    idx = (n - 1) % len(alphabet)
    repeat = (n - 1) // len(alphabet) + 1
    return alphabet[idx] * repeat


def format_number(n: int, fmt: str) -> str | None:
    match fmt:
        case "decimal":
            return str(n)
        case "decimalZero":
            return f"{n:02d}"
        case "lowerLetter":
            return _letters(n, "abcdefghijklmnopqrstuvwxyz")
        case "upperLetter":
            return _letters(n, "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        case "lowerRoman":
            return _roman(n).lower()
        case "upperRoman":
            return _roman(n)
        case "russianLower":
            return _letters(n, _RU_LOWER)
        case "russianUpper":
            return _letters(n, _RU_LOWER.upper())
        case _:
            return None


class Numbering:
    def __init__(self, root: etree._Element | None) -> None:
        self.abstracts: dict[str, AbstractNum] = {}
        self.nums: dict[str, NumInstance] = {}
        # Счётчики ведутся по abstractNum: экземпляры одного списка продолжают нумерацию,
        # если не задан startOverride.
        self.counters: dict[str, dict[int, int]] = {}
        self._overrides_applied: set[tuple[str, int]] = set()
        if root is not None:
            self._load(root)

    def _load(self, root: etree._Element) -> None:
        for an in root.iter(w("abstractNum")):
            aid = wval(an, "abstractNumId")
            if aid is None:
                continue
            abstract = AbstractNum()
            if an.find(w("numStyleLink")) is not None or an.find(w("styleLink")) is not None:
                abstract.supported = False
            for lvl in an.findall(w("lvl")):
                ilvl = _to_int(wval(lvl, "ilvl"), -1)
                if not 0 <= ilvl <= 8:
                    continue
                if lvl.find(w("isLgl")) is not None or lvl.find(w("lvlRestart")) is not None:
                    abstract.supported = False
                abstract.levels[ilvl] = Level(
                    fmt=wval(lvl.find(w("numFmt"))) or "decimal",
                    text=wval(lvl.find(w("lvlText"))) or "",
                    start=_to_int(wval(lvl.find(w("start"))), 1),
                )
            self.abstracts[aid] = abstract
        for num in root.iter(w("num")):
            nid = wval(num, "numId")
            aid = wval(num.find(w("abstractNumId")))
            if nid is None or aid is None:
                continue
            inst = NumInstance(abstract_id=aid)
            for ov in num.findall(w("lvlOverride")):
                ilvl = _to_int(wval(ov, "ilvl"), -1)
                start = ov.find(w("startOverride"))
                if 0 <= ilvl <= 8 and start is not None:
                    inst.start_overrides[ilvl] = _to_int(wval(start), 1)
            self.nums[nid] = inst

    def next_label(self, num_id: str, ilvl: int) -> ListLabel | None:
        """Продвигает счётчик и возвращает метку абзаца. None — номер не вычисляется надёжно."""
        inst = self.nums.get(num_id)
        if inst is None:
            return None
        abstract = self.abstracts.get(inst.abstract_id)
        if abstract is None or ilvl not in abstract.levels:
            return None
        counters = self.counters.setdefault(inst.abstract_id, {})
        level = abstract.levels[ilvl]
        override_key = (num_id, ilvl)
        if ilvl in inst.start_overrides and override_key not in self._overrides_applied:
            self._overrides_applied.add(override_key)
            counters[ilvl] = inst.start_overrides[ilvl]
        elif ilvl in counters:
            counters[ilvl] += 1
        else:
            counters[ilvl] = level.start
        for deeper in [k for k in counters if k > ilvl]:
            del counters[deeper]
        if not abstract.supported:
            return None
        if level.fmt == "bullet":
            return ListLabel(label="•", numeric=False)
        if level.fmt == "none":
            return ListLabel(label="", numeric=False)
        numeric = True
        label = level.text
        for ref in range(9):
            placeholder = f"%{ref + 1}"
            if placeholder not in label:
                continue
            ref_level = abstract.levels.get(ref)
            if ref_level is None:
                return None
            value = counters.get(ref, ref_level.start)
            rendered = format_number(value, ref_level.fmt)
            if rendered is None:
                return None
            numeric = numeric and ref_level.fmt in _NUMERIC_FORMATS
            label = label.replace(placeholder, rendered)
        return ListLabel(label=label, numeric=numeric)
