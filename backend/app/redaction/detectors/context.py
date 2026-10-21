"""Контекстные детекторы: адреса (физлиц и организаций) и особые категории персональных данных."""

from __future__ import annotations

import re
from collections.abc import Iterator

from app.redaction.detectors.base import TIER_CONTEXT, TIER_WEAK, Hit, before, norm_space, strip_span

_ADDR_INTRO = re.compile(
    r"(?:(?:юридическ|фактическ|почтов)\w*\s+адрес\w*|адрес\w*(?:\s+(?:места\s+нахождения|регистрации|проживания"
    r"|местонахождения|доставки|для\s+(?:направления\s+)?корреспонденции|объекта))?|место\s*нахождени\w*|"
    r"местонахождени\w*|зарегистрирован\w*(?:\s+по\s+месту\s+жительства)?|проживающ\w*)"
    r"(?:\s+по\s+адресу)?\s*[:\-–—]?\s*",
    re.IGNORECASE,
)
_ADDR_STOP = re.compile(
    r";|\n|\s(?:ИНН|ОГРН|КПП|БИК|ОКПО|тел\.?|телефон|факс|e-?mail|эл\.\s*почт|р/с|к/с|паспорт|в\s+лице|именуем)",
    re.IGNORECASE,
)
_ADDR_MARKER = re.compile(
    r"\bул\.|\bулиц|\bг\.|\bгород|\bобл\.?|област|\bпр-т|\bпросп|\bпер\.|переул|\bд\.\s*\d|\bдом\b|\bкв\.|"
    r"\bофис|\bпом\.|помещени|\bресп|\bкрай\b|район|\bр-н|\bпос\.|\bс\.\s|шоссе|\bнаб\.|набережн|\bмкр|"
    r"(?<!\d)\d{6}(?!\d)",
    re.IGNORECASE,
)
_STREET = re.compile(
    r"(?:\bул\.|\bулица|\bпр-т|\bпросп\.|\bпроспект|\bпер\.|\bпереулок|\bш\.|\bшоссе|\bб-р|\bбульвар|\bнаб\.|"
    r"\bнабережная|\bпл\.|\bплощадь|\bпроезд|\bмкр\.?|\bмикрорайон)\s*[А-ЯЁA-Z0-9][^,;\n]{0,60}?,?\s*(?:д\.|дом)\s*"
    r"\d+[\wА-Яа-яЁё/-]*"
)
_ADDR_TAIL = re.compile(
    r"(?:,?\s*(?:корп\.|корпус|к\.|стр\.|строение|лит\.|литера|кв\.|квартира|оф\.|офис|пом\.|помещение|ком\.|"
    r"комната|эт\.|этаж)\s*[\wА-Яа-яЁё/-]+)*"
)
_HOUSE = re.compile(r"(?:\bд\.|\bдом)\s*\d+[\wА-Яа-яЁё/-]*")
_ADDR_HEAD = re.compile(
    r"(?<!\d)\d{6}(?!\d)|\bг\.|\bгород|\bобл|област|\bкрай\b|\bресп|Республик|\bр-н|район|\bпос\.|\bп\.|\bс\.|"
    r"Российская|Россия|\bРФ\b"
)
_PERSON_ADDRESS = re.compile(
    r"зарегистрирован|проживающ|места\s+жительства|регистрации|паспорт|гражданин|\bгр\."
)


def _address_hit(text: str, start: int, end: int, confidence: str) -> Hit | None:
    start, end = strip_span(text, start, end, " ,.;:-–—")
    if end - start < 8:
        return None
    body = text[start:end]
    if not _ADDR_MARKER.search(body) or not any(c.isdigit() for c in body):
        return None
    category = "address_person" if _PERSON_ADDRESS.search(before(text, start, 100)) else "address_org"
    key = "address:" + norm_space(body).lower().replace("ё", "е")
    return Hit(start, end, category, key, confidence=confidence, tier=TIER_CONTEXT)


def addresses(text: str) -> Iterator[Hit]:
    taken: list[tuple[int, int]] = []
    for m in _ADDR_INTRO.finditer(text):
        stop = _ADDR_STOP.search(text, m.end())
        end = min(stop.start() if stop else len(text), m.end() + 250)
        # Есть номер дома — адрес заканчивается на нём (и корпусе/квартире/офисе после него).
        house = _HOUSE.search(text, m.end(), end)
        if house:
            tail = _ADDR_TAIL.match(text, house.end())
            end = min(end, tail.end() if tail else house.end())
        hit = _address_hit(text, m.end(), end, "medium")
        if hit:
            taken.append((hit.start, hit.end))
            yield hit
    for m in _STREET.finditer(text):
        if any(a <= m.start() < b for a, b in taken):
            continue
        # Расширение влево до начала адреса (индекс, город, регион) в пределах той же части предложения.
        left_from = max(0, m.start() - 120)
        left = text[left_from : m.start()]
        cut = max(left.rfind(c) for c in ";:\n(")
        region = left[cut + 1 :]
        head = _ADDR_HEAD.search(region)
        start = left_from + cut + 1 + head.start() if head else m.start()
        tail = _ADDR_TAIL.match(text, m.end())
        end = tail.end() if tail else m.end()
        hit = _address_hit(text, start, end, "medium")
        if hit:
            taken.append((hit.start, hit.end))
            yield hit


_SPECIAL = re.compile(
    r"(?:диагноз\w*|заболевани\w*|инвалидност\w*|групп\w*\s+инвалидности|вероисповедани\w*|религиозн\w+\s+"
    r"убеждени\w*|национальност\w*|расов\w+\s+принадлежност\w*|судимост\w*|состояни\w+\s+здоровья)\s*[:\-—]\s*"
    r"([^;.\n]{2,80})",
    re.IGNORECASE,
)
_DISABILITY = re.compile(r"инвалид\w*\s+(?:I{1,3}|[123])\s+групп\w*", re.IGNORECASE)


def special_categories(text: str) -> Iterator[Hit]:
    for m in _SPECIAL.finditer(text):
        start, end = strip_span(text, m.start(1), m.end(1))
        if end > start:
            key = "special:" + norm_space(text[start:end]).lower()
            yield Hit(
                start, end, "special_category", key, source="dictionary", confidence="low", tier=TIER_WEAK
            )
    for m in _DISABILITY.finditer(text):
        key = "special:" + norm_space(m.group(0)).lower()
        yield Hit(
            m.start(), m.end(), "special_category", key, source="dictionary", confidence="low", tier=TIER_WEAK
        )
