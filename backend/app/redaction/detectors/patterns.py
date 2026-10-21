"""Шаблоны с контрольными суммами и точными форматами: реквизиты, документы, контакты, коммерческие и
технические данные.

Правило для чисел без контрольной суммы: значение маскируется только рядом с контекстным словом
(«ИНН», «КПП», «паспорт»…), иначе цифры договора (суммы, сроки, номера пунктов) были бы скрыты.
"""

from __future__ import annotations

import ipaddress
import math
import re
from collections import Counter
from collections.abc import Iterator

import phonenumbers

from app.redaction import checksums
from app.redaction.detectors.base import (
    DATE_RE,
    TIER_CONTEXT,
    Hit,
    before,
    digits_of,
    norm_space,
    parse_date,
    strip_span,
)

_D = r"(?<!\d)"
_ND = r"(?!\d)"
_NUM_SIGN = r"(?:№|N|No\.?|номер)?"


def _ctx(text: str, start: int, pattern: re.Pattern[str], width: int = 40) -> bool:
    return pattern.search(before(text, start, width)) is not None


# ------------------------------------------------------------------ реквизиты организаций и лиц

_INN_CTX = re.compile(r"инн")
_KPP_CTX = re.compile(r"кпп")
_OGRN_CTX = re.compile(r"огрн")
_BIK_CTX = re.compile(r"бик")
_OKPO_CTX = re.compile(r"окпо|октмо|окато|оквэд|окогу|окфс|окопф")
_SNILS_CTX = re.compile(r"снилс|страхов\w* номер")
_ACCOUNT_CTX = re.compile(r"р/с|к/с|л/с|сч[её]т|account")
_CARD_CTX = re.compile(r"карт")
_OMS_CTX = re.compile(r"полис|омс")

_INN_RE = re.compile(_D + r"(\d{12}|\d{10})" + _ND)
_OGRN_RE = re.compile(_D + r"(\d{15}|\d{13})" + _ND)
_KPP_RE = re.compile(_D + r"(\d{4}[\dA-Z]{2}\d{3})" + _ND)
_BIK_RE = re.compile(_D + r"(\d{9})" + _ND)
_OKPO_RE = re.compile(_D + r"(\d{11}|\d{10}|\d{8})" + _ND)
_SNILS_RE = re.compile(_D + r"(\d{3}-\d{3}-\d{3}[ -]\d{2}|\d{11})" + _ND)
_ACCOUNT_RE = re.compile(_D + r"(\d{5}[ .]?\d{3}[ .]?\d[ .]?\d{4}[ .]?\d{7})" + _ND)
_CARD_RE = re.compile(_D + r"((?:\d{4}[ -]?){3}\d{4}(?:[ -]?\d{1,3})?)" + _ND)
_IBAN_RE = re.compile(r"\b([A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?)\b")
_SWIFT_RE = re.compile(r"\b(?:SWIFT|BIC)\b[^A-Z\n]{0,15}([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b")


def requisites(text: str, banks: set[str]) -> Iterator[Hit]:
    for m in _INN_RE.finditer(text):
        value = m.group(1)
        context = _ctx(text, m.start(), _INN_CTX)
        valid = checksums.inn_valid(value)
        category = "inn_org" if len(value) == 10 else "inn_person"
        # 10 цифр без контекста встречаются часто (телефоны, номера) — только рядом со словом «ИНН».
        if context or (valid and len(value) == 12):
            yield Hit(m.start(), m.end(), category, f"inn:{value}", confidence="high" if valid else "medium")
    for m in _OGRN_RE.finditer(text):
        value = m.group(1)
        valid = checksums.ogrn_valid(value)
        if valid or _ctx(text, m.start(), _OGRN_CTX):
            yield Hit(m.start(), m.end(), "ogrn", f"ogrn:{value}", confidence="high" if valid else "medium")
    for m in _KPP_RE.finditer(text):
        if _ctx(text, m.start(), _KPP_CTX, 30) and not _ctx(text, m.start(), _INN_CTX, 3):
            yield Hit(m.start(), m.end(), "kpp", f"kpp:{m.group(1)}")
    for m in _BIK_RE.finditer(text):
        if _ctx(text, m.start(), _BIK_CTX, 25):
            yield Hit(m.start(), m.end(), "bik", f"bik:{m.group(1)}")
    for m in _OKPO_RE.finditer(text):
        window = before(text, m.start(), 25)
        found = _OKPO_CTX.search(window)
        if found and found.group(0) in ("окпо", "октмо", "окато"):
            yield Hit(m.start(), m.end(), "okpo_oktmo_okato", f"okpo:{m.group(1)}")
    for m in _SNILS_RE.finditer(text):
        value = digits_of(m.group(1))
        context = _ctx(text, m.start(), _SNILS_CTX)
        if context or ("-" in m.group(1) and checksums.snils_valid(value)):
            yield Hit(m.start(), m.end(), "snils", f"snils:{value}")
    for m in _ACCOUNT_RE.finditer(text):
        value = digits_of(m.group(1))
        valid = any(checksums.account_key_valid(value, b) for b in banks)
        # 20 цифр подряд — практически всегда номер счёта.
        yield Hit(
            m.start(), m.end(), "bank_account", f"account:{value}", confidence="high" if valid else "medium"
        )
    for m in _CARD_RE.finditer(text):
        value = digits_of(m.group(1))
        if len(value) == 16 and _ctx(text, m.start(), _OMS_CTX):
            yield Hit(
                m.start(), m.end(), "oms_policy", f"oms:{value}", confidence="medium", tier=TIER_CONTEXT
            )
        elif len(value) <= 19 and (checksums.luhn_valid(value) or _ctx(text, m.start(), _CARD_CTX, 25)):
            yield Hit(m.start(), m.end(), "bank_card", f"card:{value}")
    for m in _IBAN_RE.finditer(text):
        value = m.group(1).replace(" ", "")
        if checksums.iban_valid(value):
            yield Hit(m.start(1), m.end(1), "iban_swift", f"iban:{value}")
    for m in _SWIFT_RE.finditer(text):
        yield Hit(m.start(1), m.end(1), "iban_swift", f"swift:{m.group(1)}")


def bank_codes(text: str) -> set[str]:
    """БИК, упомянутые в тексте (для проверки ключа счёта)."""
    return {m.group(1) for m in _BIK_RE.finditer(text) if _ctx(text, m.start(), _BIK_CTX, 25)}


# ------------------------------------------------------------------ документы физлиц и ТС

_PASSPORT_CTX = re.compile(r"паспорт")
_PASSPORT_NUM = re.compile(_D + r"(?:серия\s*)?(\d{2}\s?\d{2})\s*" + _NUM_SIGN + r"\s*(\d{6})" + _ND, re.I)
_DIVISION = re.compile(r"код\w*\s+подразделени\w*\s*[:\-]?\s*(\d{3}-\d{3})", re.I)
_ISSUED = re.compile(
    r"выдан\w*\s*[:\-]?\s*(.{5,200}?)(?=,?\s*(?:дата\s+выдачи|\d{1,2}\.\d{1,2}\.\d{2,4}|«?\d{1,2}»?\s+[а-я]+\s+\d{4}"
    r"|код\w*\s+подразд|зарегистр|проживающ|адрес|место\s+рожд|снилс|инн)|[;\n]|$)",
    re.I,
)
_ISSUE_DATE = re.compile(r"дат\w*\s+выдачи\s*[:\-]?\s*", re.I)
_FOREIGN_CTX = re.compile(r"загран|вид\w* на жительство|внж|разрешени\w* на (?:временное )?проживание")
_FOREIGN_NUM = re.compile(_D + r"(\d{2}\s?" + _NUM_SIGN + r"\s?\d{7})" + _ND, re.I)
_BIRTH_CERT = re.compile(r"\b([IVXLC]{1,4}-[А-ЯЁ]{2}\s?" + _NUM_SIGN + r"\s?\d{6})" + _ND)
_MILITARY_CTX = re.compile(r"военн\w* билет|удостоверени\w* личности")
_MILITARY_NUM = re.compile(r"\b([А-ЯЁ]{2}\s?" + _NUM_SIGN + r"\s?\d{7})" + _ND)
_LICENSE_CTX = re.compile(r"водительск|\bву\b")
_LICENSE_NUM = re.compile(_D + r"(\d{2}\s?(?:\d{2}|[А-ЯЁ]{2})\s?" + _NUM_SIGN + r"\s?\d{6})" + _ND, re.I)
_OMS_NUM = re.compile(_D + r"(\d{16}|\d{4} \d{4} \d{4} \d{4})" + _ND)
_PLATE_LETTERS = "АВЕКМНОРСТУХABEKMHOPCTYX"
_PLATE = re.compile(
    rf"(?<![\wА-ЯЁа-яё])([{_PLATE_LETTERS}]\s?\d{{3}}\s?[{_PLATE_LETTERS}]{{2}}\s?\d{{2,3}})" + _ND
)
_VIN = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b")
_VIN_CTX = re.compile(r"vin|идентификационн")
_PTS_CTX = re.compile(r"птс|стс|паспорт\w* транспортн|свидетельств\w* о регистрации т|электронн\w* паспорт")
_PTS_NUM = re.compile(_D + r"(\d{2}\s?(?:[А-ЯЁ]{2}|\d{2})\s?" + _NUM_SIGN + r"\s?\d{6})" + _ND, re.I)
_BIRTH_CTX = re.compile(r"рожд|д\.\s?р\.|г\.\s?р\.")
_BIRTH_PLACE = re.compile(
    r"мест\w*\s+рождения\s*[:\-]?\s*(.{3,150}?)(?=[;\n]|,\s*(?:паспорт|зарегистр|проживающ|адрес|гражданств|снилс|инн)|$)",
    re.I,
)


def documents(text: str) -> Iterator[Hit]:
    for m in _PASSPORT_NUM.finditer(text):
        window = before(text, m.start(), 60)
        if _PASSPORT_CTX.search(window) and not _FOREIGN_CTX.search(window) and not _PTS_CTX.search(window):
            key = f"passport:{digits_of(m.group(0))}"
            yield Hit(m.start(1), m.end(2), "passport_ru", key)
    for m in _DIVISION.finditer(text):
        yield Hit(m.start(1), m.end(1), "passport_ru", f"division:{m.group(1)}")
    for m in _ISSUED.finditer(text):
        if _PASSPORT_CTX.search(before(text, m.start(), 150)) or _FOREIGN_CTX.search(
            before(text, m.start(), 150)
        ):
            start, end = strip_span(text, m.start(1), m.end(1))
            if end - start >= 5:
                key = f"issued:{norm_space(text[start:end]).lower()}"
                yield Hit(start, end, "passport_ru", key, confidence="medium", tier=TIER_CONTEXT)
            # Дата выдачи сразу после «выдан …».
            gap = re.match(r",?\s*(?:дата\s+выдачи\s*[:\-]?\s*)?", text[m.end(1) :])
            issued = DATE_RE.match(text, m.end(1) + (gap.end() if gap else 0))
            if issued:
                yield Hit(issued.start(), issued.end(), "passport_ru", f"issue_date:{issued.group(0)}")
    for m in _ISSUE_DATE.finditer(text):
        d = DATE_RE.match(text, m.end())
        if d and _PASSPORT_CTX.search(before(text, m.start(), 150)):
            yield Hit(d.start(), d.end(), "passport_ru", f"issue_date:{d.group(0)}")
    for m in _FOREIGN_NUM.finditer(text):
        if _FOREIGN_CTX.search(before(text, m.start(), 60)):
            yield Hit(m.start(1), m.end(1), "id_document_other", f"doc:{digits_of(m.group(1))}")
    for m in _BIRTH_CERT.finditer(text):
        yield Hit(m.start(1), m.end(1), "id_document_other", f"doc:{norm_space(m.group(1))}")
    for m in _MILITARY_NUM.finditer(text):
        if _MILITARY_CTX.search(before(text, m.start(), 60)):
            yield Hit(
                m.start(1),
                m.end(1),
                "id_document_other",
                f"doc:{norm_space(m.group(1))}",
                confidence="medium",
            )
    for m in _LICENSE_NUM.finditer(text):
        if _LICENSE_CTX.search(before(text, m.start(), 60)):
            yield Hit(
                m.start(1), m.end(1), "driver_license", f"dl:{norm_space(m.group(1))}", confidence="medium"
            )
    for m in _OMS_NUM.finditer(text):
        if _OMS_CTX.search(before(text, m.start(), 40)):
            yield Hit(m.start(1), m.end(1), "oms_policy", f"oms:{digits_of(m.group(1))}", confidence="medium")
    for m in _PLATE.finditer(text):
        yield Hit(
            m.start(1), m.end(1), "vehicle", f"plate:{m.group(1).replace(' ', '')}", confidence="medium"
        )
    for m in _VIN.finditer(text):
        value = m.group(1)
        letters = sum(c.isalpha() for c in value)
        if (
            letters >= 2
            and letters <= 15
            and (checksums.vin_check_digit_valid(value) or _VIN_CTX.search(before(text, m.start(), 30)))
        ):
            yield Hit(m.start(1), m.end(1), "vehicle", f"vin:{value}")
    for m in _PTS_NUM.finditer(text):
        if _PTS_CTX.search(before(text, m.start(), 60)):
            yield Hit(m.start(1), m.end(1), "vehicle", f"pts:{norm_space(m.group(1))}", confidence="medium")
    for m in _BIRTH_PLACE.finditer(text):
        start, end = strip_span(text, m.start(1), m.end(1))
        if end - start >= 3:
            key = f"birthplace:{norm_space(text[start:end]).lower()}"
            yield Hit(start, end, "birth_place", key, confidence="low", tier=TIER_CONTEXT)


def dates(text: str, *, all_dates: bool) -> Iterator[Hit]:
    """Даты рождения (по контексту) и, если включена категория `dates`, — все остальные даты."""
    for m in DATE_RE.finditer(text):
        parsed = parse_date(m)
        if parsed is None:
            continue
        start, end = strip_span(text, m.start(), m.end(), " «»")
        if _BIRTH_CTX.search(before(text, m.start(), 30)):
            yield Hit(start, end, "birth_date", f"date:{parsed.isoformat()}", confidence="medium")
        elif all_dates:
            yield Hit(start, end, "dates", f"date:{parsed.isoformat()}")


# ------------------------------------------------------------------ контакты

_EMAIL = re.compile(r"(?<![\w.+-])([A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-zА-Яа-яЁё]{2,})(?![\w-])")
_PHONE_CTX = re.compile(r"тел|факс|моб|контактн|phone|fax|whatsapp|telegram")
_REQUISITE_BEFORE = re.compile(r"инн|огрн|кпп|бик|окпо|сч[её]т|р/с|к/с|№")


def contacts(text: str) -> Iterator[Hit]:
    for m in _EMAIL.finditer(text):
        yield Hit(m.start(1), m.end(1), "email", f"email:{m.group(1).lower()}")
    for match in phonenumbers.PhoneNumberMatcher(text, "RU", leniency=phonenumbers.Leniency.VALID):
        raw = match.raw_string
        if _REQUISITE_BEFORE.search(before(text, match.start, 12)):
            continue
        formatted = any(c in raw for c in "+()") or (
            any(c in raw for c in " -") and len(digits_of(raw)) >= 10
        )
        if formatted or _PHONE_CTX.search(before(text, match.start, 30)):
            e164 = phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.E164)
            yield Hit(match.start, match.end, "phone", f"phone:{e164}")


# ------------------------------------------------------------------ коммерческие данные

_AMOUNT_NUMBER = r"\d{1,3}(?:[   ]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
_CURRENCY = r"(?:руб(?:лей|ля|ль)?\.?|₽|RUB|USD|EUR|CNY|долл(?:аров|ара|ар)?\.?(?:\s+США)?|евро|юан\w*|\$|€)"
_MONEY = re.compile(
    rf"(?<![\d.,])(?:{_AMOUNT_NUMBER})(?:\s*\([^()\n]{{3,200}}\))?\s*{_CURRENCY}(?![\w])"
    r"(?:\s*\d{1,2}\s*(?:\([^()\n]{2,40}\)\s*)?коп(?:еек|ейки|ейка)?\.?)?",
    re.IGNORECASE,
)
_RATE = re.compile(
    r"(?<![\d.,])\d+(?:[.,]\d+)?\s*(?:\([^()\n]{2,80}\)\s*)?(?:%|процент(?:ов|а)?(?![\w]))",
    re.IGNORECASE,
)
_DOC_WORDS = (
    r"договор\w*|контракт\w*|приложени\w*|дополнительн\w+\s+соглашени\w*|допсоглашени\w*|соглашени\w*"
    r"|заказ\w*|сч[её]т\w*(?:-фактур\w*)?|спецификаци\w*|акт\w*|заявк\w*|накладн\w*|протокол\w*"
)
_CONTRACT_NO = re.compile(
    rf"(?:{_DOC_WORDS})\s+(?:№|N|No\.?)\s*([\wА-Яа-яЁё][\wА-Яа-яЁё/.\-]*)", re.IGNORECASE
)
_POA_NO = re.compile(r"доверенност\w*\s+((?:№|N|No\.?)\s*[\wА-Яа-яЁё][\wА-Яа-яЁё/.\-]*)", re.IGNORECASE)
_POA_DATE = re.compile(r"доверенност\w*\s+(от\s+)", re.IGNORECASE)
_CADASTRAL = re.compile(_D + r"(\d{2}:\d{2}:\d{6,7}:\d{1,6})" + _ND)


def commercial(text: str) -> Iterator[Hit]:
    for m in _MONEY.finditer(text):
        yield Hit(
            m.start(), m.end(), "money_amount", f"money:{norm_space(m.group(0)).lower()}", confidence="medium"
        )
    for m in _RATE.finditer(text):
        yield Hit(
            m.start(), m.end(), "rate_percent", f"rate:{norm_space(m.group(0)).lower()}", confidence="medium"
        )
    for m in _CONTRACT_NO.finditer(text):
        start, end = strip_span(text, m.start(1), m.end(1), ".,;:-/")
        if end > start:
            yield Hit(start, end, "contract_number", f"number:{text[start:end]}", confidence="medium")
    for m in _CADASTRAL.finditer(text):
        yield Hit(m.start(1), m.end(1), "cadastral_number", f"cadastral:{m.group(1)}")


def powers_of_attorney(text: str) -> Iterator[Hit]:
    """Реквизиты доверенности: «№ 12/3 от 01.02.2024» или «от 01.02.2024 № 5»."""
    for m in _POA_NO.finditer(text):
        start, end = m.start(1), m.end(1)
        rest = re.match(r"\s+от\s+", text[end:])
        if rest:
            d = DATE_RE.match(text, end + rest.end())
            if d:
                end = d.end()
        start, end = strip_span(text, start, end, " .,;:")
        yield Hit(
            start, end, "power_of_attorney", f"poa:{norm_space(text[start:end]).lower()}", confidence="medium"
        )
    for m in _POA_DATE.finditer(text):
        d = DATE_RE.match(text, m.end())
        if not d:
            continue
        end = d.end()
        num = re.match(r"\s*(?:№|N)\s*[\wА-Яа-яЁё][\wА-Яа-яЁё/.\-]*", text[end:])
        if num:
            end += num.end()
        start, end = strip_span(text, m.start(1), end, " .,;:")
        yield Hit(
            start, end, "power_of_attorney", f"poa:{norm_space(text[start:end]).lower()}", confidence="medium"
        )


# ------------------------------------------------------------------ технические данные

_URL = re.compile(
    r"\bhttps?://[^\s<>\"«»]+|\bwww\.[^\s<>\"«»]+"
    r"|(?<![@\w.-])(?:[a-z0-9-]+\.)+(?:ru|рф|com|org|net|su|info|io|biz|pro|online|site|tech|ai|dev|app|by|kz)\b"
    r"(?:/[^\s<>\"«»]*)?",
    re.IGNORECASE,
)
_IPV4 = re.compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?![\d.])")
_IPV6 = re.compile(r"(?<![\w:])((?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4})(?![\w:])")
_IP_CTX = re.compile(r"\bip\b|сервер|хост|адрес")
_CLAUSE_CTX = re.compile(r"(?:п\.|пп\.|пункт\w*|подпункт\w*|раздел\w*|стать\w*|ст\.)\s*$")
_CREDENTIAL = re.compile(
    r"(?:логин|login|пароль|password|passwd|токен|token|api[ _-]?key|ключ\w*\s+(?:api|доступа)"
    r"|secret|секрет\w*)"
    r"\s*[:=]\s*([^\s,;]{4,120})",
    re.IGNORECASE,
)
_TOKEN_LIKE = re.compile(r"(?<![\w+/=-])([A-Za-z0-9_\-+/=]{24,200})(?![\w+/=-])")


def _entropy(value: str) -> float:
    counts = Counter(value)
    return -sum(n / len(value) * math.log2(n / len(value)) for n in counts.values())


def technical(text: str, *, urls: bool, ips: bool, credentials: bool) -> Iterator[Hit]:
    if urls:
        for m in _URL.finditer(text):
            start, end = strip_span(text, m.start(), m.end(), ".,;:)!?»\"'")
            yield Hit(start, end, "url_domain", f"url:{text[start:end].lower()}")
    if ips:
        for m in _IPV4.finditer(text):
            value = m.group(1)
            window = before(text, m.start(), 25)
            if _CLAUSE_CTX.search(window):
                continue
            try:
                ipaddress.IPv4Address(value)
            except ValueError:
                continue
            if _IP_CTX.search(window) or max(int(p) for p in value.split(".")) >= 100:
                yield Hit(m.start(1), m.end(1), "ip_address", f"ip:{value}")
        for m in _IPV6.finditer(text):
            try:
                ipaddress.IPv6Address(m.group(1))
            except ValueError:
                continue
            yield Hit(m.start(1), m.end(1), "ip_address", f"ip:{m.group(1).lower()}")
    if credentials:
        for m in _CREDENTIAL.finditer(text):
            yield Hit(m.start(1), m.end(1), "credentials", f"secret:{m.group(1)}", confidence="medium")
        for m in _TOKEN_LIKE.finditer(text):
            value = m.group(1)
            classes = sum(
                1 for check in (str.isupper, str.islower, str.isdigit) if any(check(c) for c in value)
            )
            if classes >= 3 and _entropy(value) >= 3.5:
                yield Hit(m.start(1), m.end(1), "credentials", f"secret:{value}", confidence="medium")
