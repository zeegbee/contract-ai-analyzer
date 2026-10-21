"""NER (slovnet + navec из пакета natasha, MIT) и морфология (pymorphy3, MIT) — ленивая загрузка.

Пакет `natasha` целиком не импортируется: на Python 3.13 его морфология (pymorphy2) не работает.
Из него берутся только файлы моделей (каталог `natasha/data`), находятся без импорта пакета. Модели
устанавливаются вместе с пакетом (в образе) — загрузки из сети при работе нет.
"""

from __future__ import annotations

import functools
import importlib.util
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_NAVEC_FILE = "emb/navec_news_v1_1B_250K_300d_100q.tar"
_NER_FILE = "model/slovnet_ner_news_v1.tar"
_WORD = re.compile(r"[A-Za-zА-Яа-яЁё]+(?:-[A-Za-zА-Яа-яЁё]+)*")


@dataclass(frozen=True)
class Span:
    type: str  # PER | ORG | LOC
    start: int
    end: int


def _data_dir() -> Path | None:
    spec = importlib.util.find_spec("natasha")
    if spec is None or not spec.submodule_search_locations:
        return None
    path = Path(next(iter(spec.submodule_search_locations))) / "data"
    return path if (path / _NAVEC_FILE).is_file() and (path / _NER_FILE).is_file() else None


@functools.cache
def _ner() -> Any | None:
    data = _data_dir()
    if data is None:
        return None
    try:
        from navec import Navec  # noqa: PLC0415 - тяжёлый импорт только при необходимости
        from slovnet import NER  # noqa: PLC0415
    except ImportError:
        return None
    navec = Navec.load(str(data / _NAVEC_FILE))
    model = NER.load(str(data / _NER_FILE))
    model.navec(navec)
    return model


def ner_available() -> bool:
    return _ner() is not None


def ner_spans(text: str) -> list[Span]:
    model = _ner()
    if model is None or not text.strip():
        return []
    markup = model(text)
    return [Span(s.type, s.start, s.stop) for s in markup.spans]


@functools.cache
def _morph() -> Any | None:
    try:
        import pymorphy3  # noqa: PLC0415
    except ImportError:
        return None
    return pymorphy3.MorphAnalyzer()


@functools.lru_cache(maxsize=50_000)
def lemma(word: str) -> str:
    morph = _morph()
    if morph is None:
        return word.lower()
    return str(morph.parse(word)[0].normal_form).replace("ё", "е")


@functools.lru_cache(maxsize=50_000)
def name_parts(word: str) -> frozenset[str]:
    """Граммемы имени для слова: Surn, Name, Patr (если морфология их допускает)."""
    morph = _morph()
    if morph is None:
        return frozenset()
    tags: set[str] = set()
    for p in morph.parse(word):
        for g in ("Surn", "Name", "Patr"):
            if g in p.tag:
                tags.add(g)
    return frozenset(tags)


_PROPER = frozenset({"Surn", "Name", "Patr", "Geox", "Orgn", "Trad", "Abbr"})


@functools.lru_cache(maxsize=50_000)
def is_common_word(word: str) -> bool:
    """Словарное нарицательное слово («Поставщик», «Товар», «Стороны») — не имя и не название."""
    morph = _morph()
    if morph is None:
        return False
    if not morph.word_is_known(word.lower()):
        return False
    return not any(_PROPER & set(p.tag.grammemes) for p in morph.parse(word))


@functools.lru_cache(maxsize=50_000)
def surname_lemma(word: str) -> str:
    morph = _morph()
    if morph is None:
        return word.lower()
    parses = morph.parse(word)
    for p in parses:
        if "Surn" in p.tag:
            return str(p.normal_form).replace("ё", "е")
    return str(parses[0].normal_form).replace("ё", "е")


def name_gender(words: list[str]) -> str | None:
    """Род по имени или отчеству (masc/femn), если определяется однозначно."""
    morph = _morph()
    if morph is None:
        return None
    for word in words:
        genders = {
            str(p.tag.gender)
            for p in morph.parse(word)
            if ("Name" in p.tag or "Patr" in p.tag) and p.tag.gender in ("masc", "femn")
        }
        if len(genders) == 1:
            return genders.pop()
    return None


def to_nominative(word: str, gender: str | None = None) -> str:
    """Слово ФИО в именительном падеже нужного рода (если не удаётся — как есть)."""
    morph = _morph()
    if morph is None:
        return word
    parses = [p for p in morph.parse(word) if {"Surn", "Name", "Patr"} & set(p.tag.grammemes)]
    if gender:
        parses = [p for p in parses if p.tag.gender is None or p.tag.gender == gender] or parses
    if any(p.tag.case == "nomn" for p in parses):
        return word
    for p in parses:
        form = p.inflect({"nomn"})
        if form is not None:
            result = str(form.word)
            return result[:1].upper() + result[1:] if word[:1].isupper() else result
    return word


def words(text: str) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), m.group(0)) for m in _WORD.finditer(text)]
