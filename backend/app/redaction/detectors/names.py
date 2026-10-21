"""ФИО, подписанты, наименования организаций и банков: NER + шаблоны + морфология.

Ключ ФИО — лемма фамилии и инициалы (`person:иванов:и:п`): «Иванова Ивана Петровича», «Иванов И.П.»
и «И.П. Иванову» получают один ключ. Неполные ключи (без инициалов) связываются в `linking`.
Из наименования организации маскируется только само название, организационно-правовая форма остаётся
(«ООО «⟦ОРГАНИЗАЦИЯ_1⟧»): форма важна для анализа и не является конфиденциальной.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from app.redaction import nlp
from app.redaction.detectors.base import TIER_CONTEXT, Hit, after, before, norm_space

# ------------------------------------------------------------------ ФИО

_CAP = r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?"
_INITIAL = r"[А-ЯЁ]\.\s?"
_PATR = r"[А-ЯЁ][а-яё]+(?:ович|евич|ьич|ич|овн|евн|ичн|инич)[а-яё]*"
_PERSON_PATTERNS = (
    re.compile(rf"(?<![\w.])({_CAP})\s+({_INITIAL}{_INITIAL}?)(?![\w])"),  # Иванов И.И.
    re.compile(rf"(?<![\w.])({_INITIAL}{_INITIAL}?)\s?({_CAP})(?![\w])"),  # И.И. Иванов
    re.compile(rf"(?<![\w])({_CAP})\s+({_CAP})\s+({_PATR})(?![\w])"),  # Иванов Иван Иванович
    re.compile(rf"(?<![\w])({_CAP})\s+({_PATR})\s+({_CAP})(?![\w])"),  # Иван Иванович Иванов
)
_TOKEN = re.compile(r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?|[А-ЯЁ]\.")
_PATR_RE = re.compile(_PATR + "$")
# Слова, которые NER иногда принимает за имена: роли сторон, должности, термины договора.
_NOT_NAMES = frozenset(
    [
        "заказчик",
        "исполнитель",
        "подрядчик",
        "субподрядчик",
        "поставщик",
        "покупатель",
        "продавец",
        "арендатор",
        "арендодатель",
        "лицензиар",
        "лицензиат",
        "работодатель",
        "работник",
        "агент",
        "принципал",
        "клиент",
        "сторона",
        "стороны",
        "банк",
        "заемщик",
        "заёмщик",
        "займодавец",
        "кредитор",
        "должник",
        "гарант",
        "бенефициар",
        "принципал",
        "залогодатель",
        "залогодержатель",
        "поручитель",
        "комитент",
        "комиссионер",
        "доверитель",
        "поверенный",
        "оператор",
        "получатель",
        "отправитель",
        "перевозчик",
        "экспедитор",
        "генеральный",
        "директор",
        "президент",
        "председатель",
        "главный",
        "бухгалтер",
        "руководитель",
        "договор",
        "устав",
        "приложение",
    ]
)
_SIGNATORY_BEFORE = re.compile(r"в\s+лице|представител|подпис|_{3,}|/\s*$")
_SIGNATORY_AFTER = re.compile(r"^\W{0,3}действующ|^\s*/")


def _person_key(tokens: list[str]) -> str | None:
    initials = [t[0] for t in tokens if t.endswith(".")]
    names = [t for t in tokens if not t.endswith(".")]
    # Каждое слово — имя/фамилия/отчество или незнакомое словарю слово (редкая фамилия), но не роль
    # стороны и не нарицательное слово («Поставщик», «Товар», «Стороны»).
    if not names or any(nlp.lemma(n) in _NOT_NAMES or nlp.is_common_word(n) for n in names):
        return None
    if not initials and not any(nlp.name_parts(n) for n in names):
        return None
    surname: str | None = None
    first: str | None = None
    patr: str | None = None
    rest: list[str] = []
    for word in names:
        parts = nlp.name_parts(word)
        if patr is None and (_PATR_RE.match(word) or ("Patr" in parts and "Surn" not in parts)):
            patr = word
        elif first is None and "Name" in parts and "Surn" not in parts and len(names) > 1:
            first = word
        else:
            rest.append(word)
    if rest:
        surname = rest[0]
        if first is None and len(rest) > 1:
            first = rest[1]
    if surname is None:
        if first is None:
            return None
        # Имя и отчество без фамилии.
        return f"person:~{nlp.lemma(first)}:{first[0].lower()}:{patr[0].lower() if patr else ''}"
    if initials:
        i1 = initials[0].lower()
        i2 = initials[1].lower() if len(initials) > 1 else ""
    else:
        i1 = first[0].lower() if first else ""
        i2 = patr[0].lower() if patr else ""
    return f"person:{nlp.surname_lemma(surname)}:{i1}:{i2}"


def _person_hit(text: str, start: int, end: int, source: str) -> Hit | None:
    tokens = _TOKEN.findall(text[start:end])
    key = _person_key(tokens)
    if key is None:
        return None
    flags = frozenset(
        {"signatory"}
        if _SIGNATORY_BEFORE.search(before(text, start, 40)) or _SIGNATORY_AFTER.search(after(text, end, 40))
        else ()
    )
    confidence = "medium" if len(tokens) >= 2 else "low"
    return Hit(
        start, end, "person_name", key, source=source, confidence=confidence, tier=TIER_CONTEXT, flags=flags
    )


def _trim_person(text: str, start: int, end: int) -> tuple[int, int] | None:
    found = list(_TOKEN.finditer(text, start, end))
    if not found:
        return None
    return found[0].start(), found[-1].end()


def persons(text: str, spans: list[nlp.Span]) -> Iterator[Hit]:
    seen: set[tuple[int, int]] = set()
    for pattern in _PERSON_PATTERNS:
        for m in pattern.finditer(text):
            if pattern is _PERSON_PATTERNS[3] and "Name" not in nlp.name_parts(m.group(1)):
                continue
            span = _trim_person(text, m.start(), m.end())
            if span and span not in seen:
                hit = _person_hit(text, *span, source="pattern")
                if hit:
                    seen.add(span)
                    yield hit
    for s in spans:
        if s.type != "PER":
            continue
        span = _trim_person(text, s.start, s.end)
        if span is None or span in seen or not text[span[0]].isupper():
            continue
        hit = _person_hit(text, *span, source="ner")
        if hit:
            seen.add(span)
            yield hit


# ------------------------------------------------------------------ организации и банки

_OPF_SHORT = (
    r"ООО|ОАО|ЗАО|ПАО|НАО|АО|НКО|АНО|ФГУП|ГУП|МУП|ФГБУ|ФГАУ|ГБУ|ГАУ|МБУ|МАУ|ГКУ|МКУ|ТСЖ|СНТ|ЖСК|КФХ|ТОО|ОДО"
)
_OPF_LONG = (
    r"(?:[Оо]бществ\w+\s+с\s+ограниченной\s+ответственностью|(?:[Пп]убличн\w+|[Нн]епубличн\w+|[Зз]акрыт\w+|"
    r"[Оо]ткрыт\w+)?\s*[Аа]кционерн\w+\s+обществ\w+|(?:[Аа]втономн\w+\s+)?[Нн]екоммерческ\w+\s+организаци\w+|"
    r"[Фф]едеральн\w+\s+государственн\w+\s+\w+\s+(?:предприяти|учреждени)\w*|[Гг]осударственн\w+\s+\w+\s+"
    r"(?:предприяти|учреждени)\w*|[Мм]униципальн\w+\s+\w+\s+(?:предприяти|учреждени)\w*|[Тт]овариществ\w+"
    r"(?:\s+собственников\s+жилья)?|[Фф]онд\w*)"
)
_QUOTED = r"\s*[«\"“„]([^»\"”“\n]{1,150})[»\"”“]"
_ORG_QUOTED = re.compile(rf"(?:\b(?:{_OPF_SHORT})\b|{_OPF_LONG}){_QUOTED}")
_ORG_BARE = re.compile(
    rf"\b(?:{_OPF_SHORT})\s+([А-ЯЁA-Z][\wА-Яа-яЁё-]+(?:\s+[А-ЯЁA-Z][\wА-Яа-яЁё-]+){{0,2}})"
)
_ORG_LATIN = re.compile(
    r"[«\"“]?([A-Z][\w&.-]*(?:\s+[A-Z][\w&.-]*){0,3})[»\"”]?,?\s+(?:LLC|Ltd\.?|GmbH|Inc\.?|Corp\.?|LLP|S\.A\.|B\.V\.)"
)
_BANK = re.compile(r"банк", re.IGNORECASE)
_BANK_CONTEXT = re.compile(r"(?:в|наименование|банк\w*)\s*(?:банк\w*)?\s*[:\-]?\s*$")
# Органы власти, суды, нормативные акты, роли — не конфиденциальны и важны для анализа.
_PUBLIC_ORG = re.compile(
    r"суд|кодекс|закон|устав|федераци|министерств|служб|инспекци|налогов|правительств|банк\w*\s+росси|"
    r"центральн\w+\s+банк|казначейств|росреестр|роспотребнадзор|роскомнадзор|фнс|пфр|фсс|сфр|мвд|"
    r"прокуратур|администраци|дум|совет\w*\s+федераци|арбитраж|президент|комисси|палат|нотари|"
    r"государствен\w+\s+реестр|егрюл|егрип|\b(?:апк|гпк|гк|нк|тк|жк|зк|коап|упк|ук)\b",
    re.IGNORECASE,
)
_OPF_PREFIX = re.compile(rf"(?:(?:{_OPF_SHORT})\b|{_OPF_LONG})\s*")


def org_key(name: str) -> str:
    return "org:" + norm_space(name.replace("«", "").replace("»", "").replace('"', "")).lower().replace(
        "ё", "е"
    )


_REQUISITE_WORDS = re.compile(r"\b(?:ИНН|КПП|БИК|ОГРН|ОКПО|р/с|к/с)\b|\d{5}")


def _org_hit(text: str, start: int, end: int, source: str, confidence: str) -> Hit | None:
    name = text[start:end].strip()
    if len(name) < 2:
        return None
    # Название в кавычках после формы собственности — явная организация; фильтры — только для NER.
    if source == "ner" and (_PUBLIC_ORG.search(name) or _REQUISITE_WORDS.search(name)):
        return None
    words = [w for _s, _e, w in nlp.words(name)]
    if not words or all(nlp.lemma(w) in _NOT_NAMES for w in words):
        return None
    # Без кавычек и формы собственности (только NER) — не название, если все слова нарицательные.
    if source == "ner" and all(nlp.is_common_word(w) for w in words):
        return None
    category = (
        "bank_name" if _BANK.search(name) or _BANK_CONTEXT.search(before(text, start, 30)) else "org_name"
    )
    return Hit(start, end, category, org_key(name), source=source, confidence=confidence, tier=TIER_CONTEXT)


def organizations(text: str, spans: list[nlp.Span]) -> Iterator[Hit]:
    seen: set[tuple[int, int]] = set()

    def emit(start: int, end: int, source: str, confidence: str) -> Iterator[Hit]:
        if (start, end) in seen:
            return
        hit = _org_hit(text, start, end, source, confidence)
        if hit:
            seen.add((start, end))
            yield hit

    for m in _ORG_QUOTED.finditer(text):
        yield from emit(m.start(1), m.end(1), "pattern", "high")
    for m in _ORG_BARE.finditer(text):
        if not any(a <= m.start(1) < b for a, b in seen):
            yield from emit(m.start(1), m.end(1), "pattern", "medium")
    for m in _ORG_LATIN.finditer(text):
        yield from emit(m.start(1), m.end(1), "pattern", "medium")
    for s in spans:
        if s.type != "ORG":
            continue
        start, end = s.start, s.end
        # Из NER-спана убираются форма собственности и кавычки — маскируется только название.
        prefix = _OPF_PREFIX.match(text, start, end)
        if prefix:
            start = prefix.end()
        while start < end and text[start] in ' «"“„(':
            start += 1
        while end > start and text[end - 1] in ' »"”).,;:':
            end -= 1
        if end - start < 3 or not any(c.isupper() for c in text[start:end]):
            continue
        if any(a <= start < b or a < end <= b for a, b in seen):
            continue
        yield from emit(start, end, "ner", "medium")
