"""Реестр категорий маскировки: метаданные для настроек и UI, префиксы меток, значения по умолчанию.

По умолчанию включены персональные данные и реквизиты; коммерческие и технические категории
выключены (маскировка сумм, ставок и дат ухудшает анализ неустоек, сроков и соразмерности).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Group(StrEnum):
    personal = "personal"
    organization = "organization"
    commercial = "commercial"
    technical = "technical"
    custom = "custom"


class Reliability(StrEnum):
    high = "high"
    medium = "medium"
    low = "low"
    unsupported = "unsupported"


@dataclass(frozen=True)
class Category:
    code: str
    title: str
    group: Group
    method: str
    reliability: Reliability
    label: str
    default_enabled: bool
    # Предупреждение в настройках (влияние на качество анализа).
    warning: str | None = None
    supported: bool = True


_COMMERCIAL_WARNING = (
    "Маскировка ухудшает анализ: модель не увидит значений (суммы, ставки, сроки) и не сможет оценить "
    "соразмерность условий."
)

CATEGORIES: tuple[Category, ...] = (
    # Персональные данные физлиц
    Category(
        "person_name",
        "ФИО (в любом падеже, фамилия с инициалами)",
        Group.personal,
        "NER + морфология + поиск найденных форм по всему тексту",
        Reliability.medium,
        "ЛИЦО",
        True,
    ),
    Category(
        "passport_ru",
        "Паспорт РФ: серия и номер, код подразделения, кем выдан",
        Group.personal,
        "шаблоны + контекстные слова",
        Reliability.high,
        "ПАСПОРТ",
        True,
    ),
    Category(
        "id_document_other",
        "Иные документы: загранпаспорт, вид на жительство, свидетельство о рождении, удостоверение",
        Group.personal,
        "шаблоны + контекст",
        Reliability.medium,
        "ДОКУМЕНТ",
        True,
    ),
    Category("snils", "СНИЛС", Group.personal, "шаблон + контрольная сумма", Reliability.high, "СНИЛС", True),
    Category(
        "inn_person",
        "ИНН физлица или ИП (12 цифр)",
        Group.personal,
        "шаблон + контрольная сумма",
        Reliability.high,
        "ИНН_ФЛ",
        True,
    ),
    Category(
        "birth_date",
        "Дата рождения",
        Group.personal,
        "контекст («рождения», «д.р.») + разбор даты",
        Reliability.medium,
        "ДАТА_РОЖДЕНИЯ",
        True,
    ),
    Category(
        "birth_place",
        "Место рождения",
        Group.personal,
        "контекст («место рождения»)",
        Reliability.low,
        "МЕСТО_РОЖДЕНИЯ",
        True,
    ),
    Category(
        "address_person",
        "Адрес регистрации или проживания",
        Group.personal,
        "адресные шаблоны (индекс, ул., д., кв.) + контекст",
        Reliability.medium,
        "АДРЕС_ФЛ",
        True,
    ),
    Category(
        "phone",
        "Телефоны (РФ и международные)",
        Group.personal,
        "phonenumbers",
        Reliability.high,
        "ТЕЛЕФОН",
        True,
    ),
    Category("email", "Адреса электронной почты", Group.personal, "шаблон", Reliability.high, "EMAIL", True),
    Category(
        "bank_card",
        "Номер банковской карты",
        Group.personal,
        "шаблон + алгоритм Луна",
        Reliability.high,
        "КАРТА",
        True,
    ),
    Category(
        "driver_license",
        "Водительское удостоверение",
        Group.personal,
        "шаблон + контекст",
        Reliability.medium,
        "ВУ",
        True,
    ),
    Category(
        "oms_policy", "Полис ОМС", Group.personal, "шаблон + контекст", Reliability.medium, "ПОЛИС", True
    ),
    Category(
        "vehicle",
        "Транспортные средства: госномер, VIN, ПТС/СТС",
        Group.personal,
        "шаблоны (VIN — контрольная цифра или контекст)",
        Reliability.medium,
        "ТС",
        True,
    ),
    Category(
        "special_category",
        "Особые категории (здоровье, религия, национальность, судимость — ст. 10 152-ФЗ)",
        Group.personal,
        "словарь ключевых слов, требует проверки",
        Reliability.low,
        "ОСОБЫЕ_ДАННЫЕ",
        True,
    ),
    Category(
        "biometric_signature",
        "Биометрия, факсимиле подписи",
        Group.personal,
        "в тексте не распознаётся",
        Reliability.unsupported,
        "БИОМЕТРИЯ",
        False,
        supported=False,
    ),
    # Организации и реквизиты
    Category(
        "org_name",
        "Наименования организаций (полные и сокращённые)",
        Group.organization,
        "NER + шаблоны организационно-правовых форм + связывание форм",
        Reliability.medium,
        "ОРГАНИЗАЦИЯ",
        True,
    ),
    Category(
        "inn_org",
        "ИНН юрлица (10 цифр)",
        Group.organization,
        "контрольная сумма + контекст",
        Reliability.high,
        "ИНН_ЮЛ",
        True,
    ),
    Category("ogrn", "ОГРН, ОГРНИП", Group.organization, "контрольная сумма", Reliability.high, "ОГРН", True),
    Category("kpp", "КПП", Group.organization, "шаблон + контекст", Reliability.high, "КПП", True),
    Category(
        "okpo_oktmo_okato",
        "ОКПО, ОКТМО, ОКАТО",
        Group.organization,
        "шаблон + контекст",
        Reliability.high,
        "КОД_СТАТ",
        True,
    ),
    Category(
        "bank_account",
        "Расчётный, корреспондентский, лицевой счёт",
        Group.organization,
        "20 цифр + контрольный ключ с БИК (если найден)",
        Reliability.high,
        "СЧЁТ",
        True,
    ),
    Category("bik", "БИК", Group.organization, "шаблон + контекст", Reliability.high, "БИК", True),
    Category(
        "iban_swift",
        "IBAN, SWIFT/BIC",
        Group.organization,
        "шаблон + контрольная сумма IBAN",
        Reliability.high,
        "IBAN",
        True,
    ),
    Category(
        "bank_name",
        "Наименование банка",
        Group.organization,
        "NER + контекст",
        Reliability.medium,
        "БАНК",
        True,
    ),
    Category(
        "address_org",
        "Юридический, фактический, почтовый адрес",
        Group.organization,
        "адресные шаблоны + контекст",
        Reliability.medium,
        "АДРЕС",
        True,
    ),
    Category(
        "signatory",
        "ФИО подписантов и представителей («в лице …», блок подписей)",
        Group.organization,
        "NER + контекст преамбулы и подписей",
        Reliability.medium,
        "ПОДПИСАНТ",
        True,
    ),
    Category(
        "power_of_attorney",
        "Реквизиты доверенностей (номер, дата)",
        Group.organization,
        "шаблон «доверенность № … от …»",
        Reliability.medium,
        "ДОВЕРЕННОСТЬ",
        True,
    ),
    # Коммерческие данные
    Category(
        "money_amount",
        "Денежные суммы (цифрами и прописью)",
        Group.commercial,
        "шаблоны",
        Reliability.medium,
        "СУММА",
        False,
        _COMMERCIAL_WARNING,
    ),
    Category(
        "rate_percent",
        "Проценты, ставки неустоек, тарифы",
        Group.commercial,
        "шаблоны",
        Reliability.medium,
        "СТАВКА",
        False,
        _COMMERCIAL_WARNING,
    ),
    Category(
        "contract_number",
        "Номера договоров, приложений, допсоглашений, заказов, счетов",
        Group.commercial,
        "шаблоны «№ …» + контекст",
        Reliability.medium,
        "НОМЕР",
        False,
        _COMMERCIAL_WARNING,
    ),
    Category(
        "dates",
        "Все прочие даты (метки сохраняют порядок дат)",
        Group.commercial,
        "разбор дат",
        Reliability.high,
        "ДАТА",
        False,
        _COMMERCIAL_WARNING,
    ),
    Category(
        "cadastral_number",
        "Кадастровые номера",
        Group.commercial,
        "шаблон NN:NN:NNNNNNN:NN",
        Reliability.high,
        "КАДАСТР",
        False,
    ),
    Category(
        "goods_and_projects",
        "Наименования товаров, объектов, проектов",
        Group.commercial,
        "только словарь Superadmin",
        Reliability.medium,
        "ОБЪЕКТ",
        False,
    ),
    # Технические
    Category("url_domain", "URL и домены", Group.technical, "шаблон", Reliability.high, "URL", False),
    Category(
        "ip_address", "IP-адреса", Group.technical, "шаблон + проверка адреса", Reliability.high, "IP", False
    ),
    Category(
        "credentials",
        "Логины, пароли, токены, ключи API",
        Group.technical,
        "контекст + энтропия",
        Reliability.medium,
        "СЕКРЕТ",
        True,
    ),
)

BY_CODE: dict[str, Category] = {c.code: c for c in CATEGORIES}
CODES: frozenset[str] = frozenset(BY_CODE)
# Порядок категорий в реестре — порядок предпочтения при пересечении равных по длине совпадений.
RANK: dict[str, int] = {c.code: i for i, c in enumerate(CATEGORIES)}


def default_enabled() -> dict[str, bool]:
    return {c.code: c.default_enabled and c.supported for c in CATEGORIES}


# Короткие названия — для строки отчёта «При анализе скрыто N значений (…)».
SHORT_TITLES: dict[str, str] = {
    "person_name": "ФИО",
    "passport_ru": "паспорт",
    "id_document_other": "иные документы",
    "snils": "СНИЛС",
    "inn_person": "ИНН физлица",
    "birth_date": "дата рождения",
    "birth_place": "место рождения",
    "address_person": "адрес физлица",
    "phone": "телефон",
    "email": "e-mail",
    "bank_card": "карта",
    "driver_license": "водительское удостоверение",
    "oms_policy": "полис ОМС",
    "vehicle": "транспортное средство",
    "special_category": "особые категории",
    "biometric_signature": "биометрия",
    "org_name": "организация",
    "inn_org": "ИНН юрлица",
    "ogrn": "ОГРН",
    "kpp": "КПП",
    "okpo_oktmo_okato": "ОКПО/ОКТМО/ОКАТО",
    "bank_account": "счёт",
    "bik": "БИК",
    "iban_swift": "IBAN/SWIFT",
    "bank_name": "банк",
    "address_org": "адрес",
    "signatory": "подписант",
    "power_of_attorney": "доверенность",
    "money_amount": "сумма",
    "rate_percent": "ставка",
    "contract_number": "номер документа",
    "dates": "дата",
    "cadastral_number": "кадастровый номер",
    "goods_and_projects": "объект",
    "url_domain": "URL",
    "ip_address": "IP-адрес",
    "credentials": "учётные данные",
}


def redaction_line(info: dict[str, object] | None) -> str | None:
    """Строка для отчёта и экспорта (без значений)."""
    if not info:
        return None
    counts = info.get("counts")
    counts = counts if isinstance(counts, dict) else {}
    categories = counts.get("categories")
    categories = categories if isinstance(categories, dict) else {}
    listed = ", ".join(f"{SHORT_TITLES.get(str(c), str(c))} — {n}" for c, n in sorted(categories.items()))
    values = counts.get("values", 0)
    if info.get("mode") == "unmasked_by_user":
        return "Текст отправлен облачной модели без маскировки (подтверждено пользователем)" + (
            f"; не скрыто значений: {values} ({listed})" if values else ""
        )
    return f"При анализе скрыто значений: {values}" + (f" ({listed})" if listed else "")
