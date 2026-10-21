"""Обнаружение: фрагменты версии → сущности с вхождениями (выполняется в дочернем процессе worker'а).

1. Детекторы по каждому фрагменту (шаблоны, NER, контекст, правила Superadmin).
2. Отбор по включённым категориям и списку исключений.
3. Разрешение пересечений: важнее уровень (`tier`), затем длина; объемлющее совпадение поглощает
   вложенные (скрывает больше), частичное пересечение объединяется — так ничего не остаётся открытым.
4. Связывание вхождений в сущности (одна сущность — одна метка), для ФИО — по фамилии и инициалам.
5. Распространение: найденные формы ищутся по всему тексту (если NER пропустил вхождение).

Результат детерминирован: одинаковый текст, настройки и `REDACTION_VERSION` дают одинаковый результат.
Значения в логи не пишутся.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from app.redaction import nlp
from app.redaction.categories import RANK
from app.redaction.detectors import context, names, patterns
from app.redaction.detectors.base import CONFIDENCE_RANK, TIER_WEAK, Hit, norm_space
from app.redaction.detectors.dictionary import AllowList, CustomPattern, Dictionary, DictionaryEntry, Patterns

_EXPLICIT_SOURCES = frozenset({"dictionary", "custom_pattern", "manual"})
_NER_CATEGORIES = frozenset({"person_name", "signatory", "org_name", "bank_name"})
_MIN_PROPAGATE_CHARS = 3
_SOURCE_RANK = {"manual": 5, "custom_pattern": 4, "dictionary": 4, "pattern": 3, "ner": 2, "propagated": 1}


@dataclass(frozen=True)
class ScanConfig:
    enabled: frozenset[str]
    dictionary: tuple[DictionaryEntry, ...] = ()
    patterns: tuple[CustomPattern, ...] = ()
    allow_list: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> ScanConfig:
        return cls(
            enabled=frozenset(value.get("enabled", [])),
            dictionary=tuple(DictionaryEntry(e["text"], e["category"]) for e in value.get("dictionary", [])),
            patterns=tuple(CustomPattern(p["pattern"], p["category"]) for p in value.get("patterns", [])),
            allow_list=tuple(value.get("allow_list", [])),
        )


@dataclass
class _Found:
    fragment: int
    hit: Hit
    text: str


@dataclass
class _Entity:
    key: str
    found: list[_Found] = field(default_factory=list)


# ------------------------------------------------------------------ шаг 1–3: совпадения по фрагменту


def _detect(
    text: str, config: ScanConfig, banks: set[str], dictionary: Dictionary, custom: Patterns
) -> list[Hit]:
    on = config.enabled
    hits: list[Hit] = []
    spans = nlp.ner_spans(text) if on & _NER_CATEGORIES else []
    if on & {"inn_org", "inn_person", "ogrn", "kpp", "bik", "okpo_oktmo_okato", "snils", "bank_account",
             "bank_card", "iban_swift", "oms_policy"}:  # fmt: skip
        hits.extend(patterns.requisites(text, banks))
    if on & {"passport_ru", "id_document_other", "driver_license", "oms_policy", "vehicle", "birth_place"}:
        hits.extend(patterns.documents(text))
    if on & {"birth_date", "dates"}:
        hits.extend(patterns.dates(text, all_dates="dates" in on))
    if on & {"phone", "email"}:
        hits.extend(patterns.contacts(text))
    if on & {"money_amount", "rate_percent", "contract_number", "cadastral_number"}:
        hits.extend(patterns.commercial(text))
    if "power_of_attorney" in on:
        hits.extend(patterns.powers_of_attorney(text))
    if on & {"url_domain", "ip_address", "credentials"}:
        hits.extend(
            patterns.technical(
                text, urls="url_domain" in on, ips="ip_address" in on, credentials="credentials" in on
            )
        )
    if on & {"person_name", "signatory"}:
        hits.extend(names.persons(text, spans))
    if on & {"org_name", "bank_name"}:
        hits.extend(names.organizations(text, spans))
    if on & {"address_person", "address_org"}:
        hits.extend(context.addresses(text))
    if "special_category" in on:
        hits.extend(context.special_categories(text))
    hits.extend(dictionary.find(text))
    hits.extend(custom.find(text))
    return hits


def _wanted(hit: Hit, enabled: frozenset[str]) -> bool:
    if hit.source in _EXPLICIT_SOURCES:
        return True
    if hit.category == "person_name":
        return bool(enabled & {"person_name", "signatory"})
    if hit.category in ("org_name", "bank_name"):
        return bool(enabled & {"org_name", "bank_name"})
    if hit.category in ("address_person", "address_org"):
        return bool(enabled & {"address_person", "address_org"})
    return hit.category in enabled


def resolve(hits: Iterable[Hit]) -> list[Hit]:
    """Непересекающиеся совпадения фрагмента (см. правила в описании модуля)."""
    order = sorted(hits, key=lambda h: (h.tier, -h.length, RANK.get(h.category, 99), h.start, h.key))
    accepted: list[Hit] = []
    for h in order:
        overlapping = [a for a in accepted if a.start < h.end and h.start < a.end]
        if not overlapping:
            accepted.append(h)
        elif any(a.start <= h.start and h.end <= a.end for a in overlapping):
            continue
        elif all(h.start <= a.start and a.end <= h.end for a in overlapping):
            for a in overlapping:
                accepted.remove(a)
            accepted.append(h)
        else:
            first = overlapping[0]
            accepted.remove(first)
            accepted.append(replace(first, start=min(first.start, h.start), end=max(first.end, h.end)))
    # Объединение могло создать новые пересечения — сливаем последовательно.
    merged: list[Hit] = []
    for h in sorted(accepted, key=lambda h: (h.start, h.end)):
        if merged and h.start < merged[-1].end:
            last = merged[-1]
            merged[-1] = replace(last, end=max(last.end, h.end))
        else:
            merged.append(h)
    return merged


# ------------------------------------------------------------------ шаг 4: связывание


def _person_parts(key: str) -> tuple[str, str, str] | None:
    parts = key.split(":")
    if len(parts) != 4 or parts[0] != "person" or parts[1].startswith("~"):
        return None
    return parts[1], parts[2], parts[3]


def link_person_keys(keys: Iterable[str]) -> dict[str, str]:
    """Неполный ключ ФИО (без инициалов или с одним) → единственный совместимый более полный ключ."""
    by_surname: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for key in set(keys):
        parts = _person_parts(key)
        if parts:
            by_surname[parts[0]].append(parts)
    result: dict[str, str] = {}
    for surname, items in by_surname.items():
        for _s, i1, i2 in items:
            filled = bool(i1) + bool(i2)
            candidates = {
                (a, b)
                for _x, a, b in items
                if bool(a) + bool(b) > filled and (not i1 or a == i1) and (not i2 or b == i2)
            }
            if not candidates:
                continue
            best = max(bool(a) + bool(b) for a, b in candidates)
            top = [c for c in candidates if bool(c[0]) + bool(c[1]) == best]
            if len(top) == 1:
                result[f"person:{surname}:{i1}:{i2}"] = f"person:{surname}:{top[0][0]}:{top[0][1]}"
    return result


# ------------------------------------------------------------------ шаг 5: распространение

_INITIALS_AROUND = re.compile(r"\s?(?:[А-ЯЁ]\.\s?){1,2}")


def _propagate_person(texts: Sequence[str], key: str, occupied: list[list[tuple[int, int]]]) -> list[_Found]:
    parts = _person_parts(key)
    if parts is None:
        return []
    surname = parts[0]
    found: list[_Found] = []
    for index, text in enumerate(texts):
        for word_start, word_end, word in nlp.words(text):
            if not word[:1].isupper() or len(word) < 3 or nlp.surname_lemma(word) != surname:
                continue
            if "Surn" not in nlp.name_parts(word) and nlp.lemma(word) != surname:
                continue
            # Захватываем соседние инициалы: «Иванов И.И.» / «И.И. Иванов».
            tail = _INITIALS_AROUND.match(text, word_end)
            end = tail.end() if tail else word_end
            head = re.search(r"(?:[А-ЯЁ]\.\s?){1,2}$", text[max(0, word_start - 6) : word_start])
            start = max(0, word_start - 6) + head.start() if head else word_start
            while end > start and text[end - 1] == " ":
                end -= 1
            if any(a < end and start < b for a, b in occupied[index]):
                continue
            hit = Hit(
                start, end, "person_name", key, source="propagated", confidence="medium", tier=TIER_WEAK
            )
            occupied[index].append((start, end))
            found.append(_Found(index, hit, text[start:end]))
    return found


def _propagate_surface(
    texts: Sequence[str], entity: _Entity, occupied: list[list[tuple[int, int]]]
) -> list[_Found]:
    category = entity.found[0].hit.category
    found: list[_Found] = []
    surfaces = sorted(
        {f.text for f in entity.found if len(f.text.strip()) >= _MIN_PROPAGATE_CHARS}, key=len, reverse=True
    )
    for surface in surfaces:
        pattern = re.compile(r"(?<![\w])" + re.escape(surface) + r"(?![\w])")
        for index, text in enumerate(texts):
            for m in pattern.finditer(text):
                if any(a < m.end() and m.start() < b for a, b in occupied[index]):
                    continue
                hit = Hit(
                    m.start(),
                    m.end(),
                    category,
                    entity.key,
                    source="propagated",
                    confidence="medium",
                    tier=TIER_WEAK,
                )
                occupied[index].append((m.start(), m.end()))
                found.append(_Found(index, hit, m.group(0)))
    return found


# ------------------------------------------------------------------ сущности


def _canonical(entity: _Entity, category: str) -> str:
    ordered = sorted(entity.found, key=lambda f: (f.fragment, f.hit.start))
    if category in ("person_name", "signatory"):
        best = max(
            ordered, key=lambda f: (len(nlp.words(f.text)), -sum(c == "." for c in f.text), -f.fragment)
        )
        tokens = re.split(r"(\s+)", norm_space(best.text))
        gender = nlp.name_gender([w for _s, _e, w in nlp.words(best.text)])
        return "".join(
            nlp.to_nominative(t, gender) if t.strip() and not t.endswith(".") else t for t in tokens
        )
    return norm_space(max(ordered, key=lambda f: len(f.text)).text)


def _entity_category(entity: _Entity, enabled: frozenset[str]) -> str | None:
    hits = [f.hit for f in entity.found]
    if any(h.source in _EXPLICIT_SOURCES for h in hits):
        return Counter(h.category for h in hits if h.source in _EXPLICIT_SOURCES).most_common(1)[0][0]
    categories = Counter(h.category for h in hits)
    if "person_name" in categories:
        signatory = any("signatory" in h.flags for h in hits)
        if signatory and "signatory" in enabled:
            return "signatory"
        return "person_name" if "person_name" in enabled else None
    if categories.keys() & {"org_name", "bank_name"}:
        preferred = "bank_name" if "bank_name" in categories else "org_name"
        if preferred in enabled:
            return preferred
        other = "org_name" if preferred == "bank_name" else "bank_name"
        return other if other in enabled else None
    if categories.keys() & {"address_person", "address_org"}:
        preferred = "address_person" if "address_person" in categories else "address_org"
        if preferred in enabled:
            return preferred
        other = "address_org" if preferred == "address_person" else "address_person"
        return other if other in enabled else None
    category = sorted(categories.items(), key=lambda kv: (-kv[1], RANK.get(kv[0], 99)))[0][0]
    return category if category in enabled else None


def scan(fragments: Sequence[tuple[str, str]], config: ScanConfig) -> dict[str, Any]:
    """fragments — (ключ фрагмента, текст). Возвращает {"entities": [...], "ner": bool}."""
    texts = [t for _k, t in fragments]
    allow = AllowList(config.allow_list)
    dictionary = Dictionary(config.dictionary)
    custom = Patterns(config.patterns)
    banks: set[str] = set()
    for text in texts:
        banks |= patterns.bank_codes(text)

    per_fragment: list[list[Hit]] = []
    for text in texts:
        hits = [
            h
            for h in _detect(text, config, banks, dictionary, custom)
            if _wanted(h, config.enabled)
            and not (h.source not in _EXPLICIT_SOURCES and allow.allows(text[h.start : h.end]))
        ]
        per_fragment.append(resolve(hits))

    person_links = link_person_keys(h.key for hits in per_fragment for h in hits)
    entities: dict[str, _Entity] = {}
    for index, hits in enumerate(per_fragment):
        for raw in hits:
            key = person_links.get(raw.key, raw.key)
            h = replace(raw, key=key) if key != raw.key else raw
            entities.setdefault(key, _Entity(key)).found.append(
                _Found(index, h, texts[index][h.start : h.end])
            )

    occupied = [[(h.start, h.end) for h in hits] for hits in per_fragment]
    for key in sorted(entities):
        entity = entities[key]
        added = _propagate_person(texts, key, occupied) if key.startswith("person:") else []
        added += _propagate_surface(texts, entity, occupied)
        entity.found.extend(f for f in added if not allow.allows(f.text))

    result: list[dict[str, Any]] = []
    ordered = sorted(entities.values(), key=lambda e: min((f.fragment, f.hit.start) for f in e.found))
    for entity in ordered:
        category = _entity_category(entity, config.enabled)
        if category is None:
            continue
        number = len(result) + 1
        entity_id = f"E{number:04d}"
        found = sorted(entity.found, key=lambda f: (f.fragment, f.hit.start))
        confidence = max((f.hit.confidence for f in found), key=lambda c: CONFIDENCE_RANK[c])
        source = max((f.hit.source for f in found), key=lambda s: _SOURCE_RANK.get(s, 0))
        result.append(
            {
                "id": entity_id,
                "key": entity.key,
                "category": category,
                "canonical": _canonical(entity, category),
                "confidence": confidence,
                "source": source,
                "masked": True,
                "occurrences": [
                    {
                        "id": f"{entity_id}.{n}",
                        "fragment": fragments[f.fragment][0],
                        "start": f.hit.start,
                        "end": f.hit.end,
                        "text": f.text,
                        "source": f.hit.source,
                        "masked": True,
                    }
                    for n, f in enumerate(found, start=1)
                ],
            }
        )
    return {"entities": result, "ner": nlp.ner_available() if config.enabled & _NER_CATEGORIES else None}
