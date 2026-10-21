"""Маскировка (этап 10): контрольные суммы, детекторы, связывание, пересечения, карта смещений."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.analysis.evidence import FragmentRef
from app.redaction import checksums
from app.redaction.categories import CODES, default_enabled
from app.redaction.detectors.base import TIER_CONTEXT, TIER_STRONG, TIER_WEAK, Hit
from app.redaction.mask import MaskView, build_mapping, mapping_counts, mask_fragment
from app.redaction.scan import ScanConfig, link_person_keys, resolve, scan
from app.redaction.settings import InvalidPattern, RedactionSettings, check_pattern

DEFAULT = ScanConfig(enabled=frozenset(k for k, v in default_enabled().items() if v))
ALL = ScanConfig(enabled=frozenset(CODES))

PREAMBLE = (
    "Общество с ограниченной ответственностью «Ромашка» (ООО «Ромашка»), в лице генерального директора "
    "Иванова Ивана Петровича, действующего на основании Устава, именуемое в дальнейшем «Заказчик», и "
    "индивидуальный предприниматель Петрова Анна Сергеевна, именуемая «Исполнитель», заключили договор."
)
CLAIMS = (
    "Претензии направляются Иванову И.П. по адресу: 123456, г. Москва, ул. Ленина, д. 5, кв. 12; "
    "тел.: +7 (495) 123-45-67, e-mail: ivanov@romashka.ru."
)
REQUISITES = (
    "Реквизиты Заказчика: ИНН 7707083893, КПП 770701001, ОГРН 1027700132195, р/с 40702810938000000001 "
    "в ПАО Сбербанк, БИК 044525225, к/с 30101810400000000225."
)
PERSON = (
    "Исполнитель: паспорт серия 45 06 № 123456, выдан ОВД района Арбат г. Москвы 01.02.2010, код "
    "подразделения 772-001, дата рождения 05.06.1985, СНИЛС 112-233-445 95, ИНН 500100732259, "
    "зарегистрирована по адресу: Московская обл., г. Химки, ул. Мира, д. 1, кв. 3."
)
SIGNATURES = "Заказчик ____________ /И.П. Иванов/        Исполнитель ____________ /Петрова А.С./"
TERMS = (
    "3.1. Заказчик уплачивает неустойку 0,1 % от суммы 150 000 (сто пятьдесят тысяч) рублей 00 копеек "
    "за каждый день просрочки. Срок поставки Товара — до 31.12.2026. Приёмка Товара по Акту № 5."
)
FRAGMENTS = [
    ("F000000", PREAMBLE),
    ("F000001", CLAIMS),
    ("F000002", REQUISITES),
    ("F000003", PERSON),
    ("F000004", SIGNATURES),
    ("F000005", TERMS),
]


def by_value(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {o["text"]: e for e in result["entities"] for o in e["occurrences"]}


# ------------------------------------------------------------------ контрольные суммы


@pytest.mark.parametrize(
    ("check", "valid", "invalid"),
    [
        (checksums.inn_valid, "7707083893", "7707083894"),
        (checksums.inn_valid, "500100732259", "500100732258"),
        (checksums.ogrn_valid, "1027700132195", "1027700132196"),
        (checksums.ogrn_valid, "304500116000157", "304500116000158"),
        (checksums.snils_valid, "11223344595", "11223344594"),
        (checksums.okpo_valid, "00032537", "00032536"),
        (checksums.luhn_valid, "4111111111111111", "4111111111111112"),
        (checksums.iban_valid, "GB82WEST12345698765432", "GB82WEST12345698765433"),
        (checksums.vin_check_digit_valid, "1M8GDM9AXKP042788", "1M8GDM9A1KP042788"),
    ],
)
def test_checksums(check: object, valid: str, invalid: str) -> None:
    assert callable(check)
    assert check(valid) is True
    assert check(invalid) is False


def test_account_key_with_bik() -> None:
    assert checksums.account_key_valid("30101810400000000225", "044525225")
    assert not checksums.account_key_valid("30101810400000000226", "044525225")
    assert not checksums.account_key_valid("123", "044525225")


def test_checksums_reject_garbage() -> None:
    for check in (checksums.inn_valid, checksums.ogrn_valid, checksums.snils_valid, checksums.luhn_valid):
        assert check("") is False
        assert check("12ab") is False
        assert check("１２３４５６７８９０") is False  # не ASCII-цифры


# ------------------------------------------------------------------ обнаружение


def test_scan_finds_personal_data_and_requisites() -> None:
    found = by_value(scan(FRAGMENTS, DEFAULT))
    expected = {
        "Ромашка": "org_name",
        "Иванова Ивана Петровича": "signatory",
        "Петрова Анна Сергеевна": "signatory",
        "123456, г. Москва, ул. Ленина, д. 5, кв. 12": "address_org",
        "+7 (495) 123-45-67": "phone",
        "ivanov@romashka.ru": "email",
        "7707083893": "inn_org",
        "770701001": "kpp",
        "1027700132195": "ogrn",
        "40702810938000000001": "bank_account",
        "Сбербанк": "bank_name",
        "044525225": "bik",
        "30101810400000000225": "bank_account",
        "45 06 № 123456": "passport_ru",
        "ОВД района Арбат г. Москвы": "passport_ru",
        "01.02.2010": "passport_ru",
        "772-001": "passport_ru",
        "05.06.1985": "birth_date",
        "112-233-445 95": "snils",
        "500100732259": "inn_person",
        "Московская обл., г. Химки, ул. Мира, д. 1, кв. 3": "address_person",
    }
    for value, category in expected.items():
        assert value in found, value
        assert found[value]["category"] == category, (value, found[value]["category"])
    # Роли сторон и термины договора не являются персональными данными.
    for word in ("Заказчик", "Исполнитель", "Товара", "Устава", "Приёмка Товара"):
        assert word not in found
    # Коммерческие категории по умолчанию выключены.
    assert "31.12.2026" not in found
    assert not any("рублей" in v for v in found)


def test_person_forms_are_linked_to_one_entity() -> None:
    found = by_value(scan(FRAGMENTS, DEFAULT))
    ivanov = found["Иванова Ивана Петровича"]
    texts = {o["text"] for o in ivanov["occurrences"]}
    assert {"Иванова Ивана Петровича", "Иванову И.П.", "И.П. Иванов"} <= texts
    assert ivanov["canonical"] == "Иванов Иван Петрович"
    petrova = found["Петрова Анна Сергеевна"]
    assert petrova["canonical"] == "Петрова Анна Сергеевна"  # род сохранён
    assert "Петрова А.С." in {o["text"] for o in petrova["occurrences"]}
    # Полное и краткое наименование — одна сущность.
    romashka = found["Ромашка"]
    assert len(romashka["occurrences"]) == 2


def test_commercial_categories_when_enabled() -> None:
    found = by_value(scan(FRAGMENTS, ALL))
    assert found["31.12.2026"]["category"] == "dates"
    assert found["0,1 %"]["category"] == "rate_percent"
    money = [v for v in found if "рублей" in v]
    assert money and found[money[0]]["category"] == "money_amount"
    assert found["5"]["category"] == "contract_number"


def test_disabled_categories_are_not_reported() -> None:
    enabled = DEFAULT.enabled - {"phone", "email", "person_name", "signatory"}
    found = by_value(scan(FRAGMENTS, ScanConfig(enabled=enabled)))
    assert "+7 (495) 123-45-67" not in found
    assert "ivanov@romashka.ru" not in found
    assert "Иванова Ивана Петровича" not in found


def test_signatory_falls_back_to_person_name() -> None:
    enabled = DEFAULT.enabled - {"signatory"}
    found = by_value(scan(FRAGMENTS, ScanConfig(enabled=enabled)))
    assert found["Иванова Ивана Петровича"]["category"] == "person_name"


def test_allow_list_and_dictionary_with_morphology() -> None:
    fragments = [
        ("F1", "Работы по проекту «Северный поток» выполняются для ООО «Ромашка»."),
        ("F2", "Результаты проекта Северный поток передаются Заказчику."),
    ]
    config = ScanConfig(
        enabled=DEFAULT.enabled,
        dictionary=(),
        allow_list=("Ромашка",),
    )
    assert "Ромашка" not in by_value(scan(fragments, config))
    from app.redaction.detectors.dictionary import DictionaryEntry

    config = ScanConfig(
        enabled=DEFAULT.enabled, dictionary=(DictionaryEntry("проект Северный поток", "goods_and_projects"),)
    )
    found = by_value(scan(fragments, config))
    # «по проекту «Северный поток»» и «проекта Северный поток» — разные падежи одной фразы.
    assert any(v.startswith("проекту") for v in found)
    assert "проекта Северный поток" in found
    assert found["проекта Северный поток"]["category"] == "goods_and_projects"


def test_custom_pattern() -> None:
    from app.redaction.detectors.dictionary import CustomPattern

    config = ScanConfig(enabled=frozenset(), patterns=(CustomPattern(r"PRJ-\d{4}", "goods_and_projects"),))
    found = by_value(scan([("F1", "Код проекта PRJ-1234 и PRJ-9."), ("F2", "Снова PRJ-1234.")], config))
    entity = found["PRJ-1234"]
    assert len(entity["occurrences"]) == 2
    assert "PRJ-9" not in found


def test_propagation_finds_missed_occurrences() -> None:
    fragments = [
        ("F1", "Арендодатель: Сидоренко Павел Игоревич, паспорт серия 45 07 № 654321."),
        ("F2", "Ключи передаются лично Сидоренко П.И. либо его представителю."),
        ("F3", "Сидоренко обязуется обеспечить доступ в помещение."),
    ]
    found = by_value(scan(fragments, DEFAULT))
    entity = found["Сидоренко Павел Игоревич"]
    texts = {o["text"] for o in entity["occurrences"]}
    assert "Сидоренко П.И." in texts
    assert "Сидоренко" in texts


def test_scan_is_deterministic() -> None:
    assert scan(FRAGMENTS, ALL) == scan(FRAGMENTS, ALL)


def test_phone_matcher_ignores_requisites() -> None:
    found = by_value(scan([("F1", "ИНН 0000000003, тел. 8 (916) 123-45-67")], DEFAULT))
    assert found["0000000003"]["category"] == "inn_org"
    assert found["8 (916) 123-45-67"]["category"] == "phone"


# ------------------------------------------------------------------ пересечения и связывание


def hit(start: int, end: int, category: str = "phone", tier: int = TIER_STRONG, key: str = "k") -> Hit:
    return Hit(start, end, category, key, tier=tier)


def test_resolve_prefers_tier_and_superset() -> None:
    # Объемлющее совпадение поглощает вложенное (скрывает больше).
    assert resolve([hit(5, 10), hit(0, 20, "address_org", TIER_CONTEXT)]) == [
        hit(0, 20, "address_org", TIER_CONTEXT)
    ]
    # Вложенное в уже принятое — отбрасывается.
    assert resolve([hit(0, 20), hit(5, 10, "email", TIER_WEAK)]) == [hit(0, 20)]
    # Частичное пересечение — объединение (ничего не остаётся открытым).
    result = resolve([hit(0, 10), hit(5, 15, "email", TIER_WEAK)])
    assert [(h.start, h.end, h.category) for h in result] == [(0, 15, "phone")]
    # Без пересечений — оба.
    assert len(resolve([hit(0, 3), hit(5, 8)])) == 2


def test_link_person_keys() -> None:
    links = link_person_keys(["person:иванов:и:п", "person:иванов::", "person:иванов:и:", "person:петров::"])
    assert links["person:иванов::"] == "person:иванов:и:п"
    assert links["person:иванов:и:"] == "person:иванов:и:п"
    assert "person:петров::" not in links
    # Два разных Иванова — неполный ключ не связывается.
    links = link_person_keys(["person:иванов:и:п", "person:иванов:а:с", "person:иванов::"])
    assert "person:иванов::" not in links


# ------------------------------------------------------------------ пользовательские шаблоны (ReDoS)


@pytest.mark.parametrize("pattern", [r"(a)\1", r"(?=a)b", r"(?<!x)y", "a*", "x|"])
def test_unsafe_or_empty_patterns_rejected(pattern: str) -> None:
    with pytest.raises(InvalidPattern):
        check_pattern(pattern)


def test_linear_time_pattern_accepted() -> None:
    check_pattern(r"(a+)+$")  # в RE2 нет катастрофического возврата
    check_pattern(r"PRJ-\d{4}")


def test_settings_schema() -> None:
    settings = RedactionSettings.model_validate(
        {"categories": {"money_amount": True, "biometric_signature": True}}
    )
    assert settings.categories["money_amount"] is True
    assert settings.categories["biometric_signature"] is False  # не поддерживается — не включается
    assert settings.categories["person_name"] is True  # значение по умолчанию
    assert settings.allow_unmasked is False
    with pytest.raises(ValueError, match="категор"):
        RedactionSettings.model_validate({"categories": {"unknown": True}})


# ------------------------------------------------------------------ таблица соответствий и смещения


def ref(seq: int, text: str, start: int) -> FragmentRef:
    return FragmentRef(uuid.uuid4(), seq, f"F{seq}", "paragraph", text, start, start + len(text), 1, 1, None)


def entity(
    category: str, *occ: tuple[str, int, int, str], key: str = "", masked: bool = True
) -> dict[str, Any]:
    return {
        "category": category,
        "key": key,
        "canonical": occ[0][3],
        "masked": masked,
        "occurrences": [
            {"fragment": f, "start": s, "end": e, "text": t, "masked": True} for f, s, e, t in occ
        ],
    }


def test_build_mapping_numbers_labels_and_orders_dates() -> None:
    entities = [
        entity("dates", ("F1", 0, 10, "31.12.2026"), key="date:2026-12-31"),
        entity("person_name", ("F0", 5, 11, "Иванов")),
        entity("dates", ("F0", 20, 30, "01.01.2025"), key="date:2025-01-01"),
        entity("person_name", ("F1", 20, 26, "Петров")),
        entity("org_name", ("F1", 40, 45, "Альфа"), masked=False),
    ]
    mapping = build_mapping(entities, {"F0": 0, "F1": 1})
    labels = {v["canonical"]: k for k, v in mapping["labels"].items()}
    assert labels["Иванов"] == "⟦ЛИЦО_1⟧" and labels["Петров"] == "⟦ЛИЦО_2⟧"
    # Порядок дат сохраняется независимо от места в тексте.
    assert labels["01.01.2025"] == "⟦ДАТА_1⟧" and labels["31.12.2026"] == "⟦ДАТА_2⟧"
    assert "Альфа" not in labels  # снятая пометка
    assert mapping_counts(mapping) == {
        "categories": {"dates": 2, "person_name": 2},
        "values": 4,
        "occurrences": 4,
    }


def test_mask_fragment_offsets_edges_and_adjacent() -> None:
    text = "Иванов Петров передал"
    mf = mask_fragment(text, [[0, 6, "⟦ЛИЦО_1⟧"], [7, 13, "⟦ЛИЦО_2⟧"]])
    assert mf.masked == "⟦ЛИЦО_1⟧ ⟦ЛИЦО_2⟧ передал"
    # Начало и конец метки → границы сущности; середина метки → вся сущность.
    assert mf.to_original(0, end=False) == 0
    assert mf.to_original(len("⟦ЛИЦО_1⟧"), end=True) == 6
    assert mf.to_original(3, end=False) == 0 and mf.to_original(3, end=True) == 6
    tail = mf.masked.index("передал")
    assert mf.to_original(tail, end=False) == text.index("передал")
    assert mf.to_original(len(mf.masked), end=True) == len(text)
    # Обратный перевод.
    assert mf.to_masked(text.index("передал"), end=False) == tail
    assert mf.to_masked(2, end=False) == 0 and mf.to_masked(2, end=True) == len("⟦ЛИЦО_1⟧")


def test_contract_text_cannot_forge_labels() -> None:
    mf = mask_fragment("Текст ⟦ЛИЦО_1⟧ из договора", [])
    assert "⟦" not in mf.masked and "⟧" not in mf.masked
    assert mf.masked == "Текст [ЛИЦО_1] из договора"


def test_mask_view_global_ranges_and_unmask() -> None:
    f0 = ref(0, "ООО «Альфа» в лице Иванова И.И.", 0)
    f1 = ref(1, "Иванов И.И. подписал акт.", f0.char_end + 2)
    entities = [
        entity("org_name", ("F0", 5, 10, "Альфа")),
        {
            **entity("person_name", ("F0", 19, 31, "Иванова И.И."), ("F1", 0, 11, "Иванов И.И.")),
            "canonical": "Иванов И.И.",
        },
    ]
    view = MaskView([f0, f1], build_mapping(entities, {"F0": 0, "F1": 1}))
    m0, m1 = view.masked
    assert m0.text == "ООО «⟦ОРГАНИЗАЦИЯ_1⟧» в лице ⟦ЛИЦО_1⟧"
    assert m1.text == "⟦ЛИЦО_1⟧ подписал акт."
    assert m1.char_start == m0.char_end + 2  # разделитель сохранён
    # Цитата через границу фрагментов: «в лице ⟦ЛИЦО_1⟧\n\n⟦ЛИЦО_1⟧ подписал».
    combined = m0.text + "\n\n" + m1.text
    start = combined.index("в лице")
    end = combined.index(" акт")
    o_start, o_end = view.to_original_range(start, end)
    original = f0.text + "\n\n" + f1.text
    assert original[o_start:o_end] == "в лице Иванова И.И.\n\nИванов И.И. подписал"
    assert view.to_masked_range(o_start, o_end) == (start, end)
    # Обратная подстановка: известные метки → канонические значения; выдуманные остаются и отмечаются.
    assert view.unmask("Риск для ⟦ОРГАНИЗАЦИЯ_1⟧ и [ЛИЦО_1]") == ("Риск для Альфа и Иванов И.И.", False)
    assert view.unmask("ЛИЦО_1 подписал") == ("Иванов И.И. подписал", False)
    assert view.unmask("Риск для ⟦ЛИЦО_7⟧") == ("Риск для ⟦ЛИЦО_7⟧", True)
    # Значения в свободном тексте (название стороны от пользователя) → метки.
    assert view.mask_free_text("ООО «альфа»") == "ООО «⟦ОРГАНИЗАЦИЯ_1⟧»"
