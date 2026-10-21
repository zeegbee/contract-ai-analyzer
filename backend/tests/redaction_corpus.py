"""Размеченный синтетический набор для оценки обнаружения (этап 10, план §9 «Качество обнаружения»).

Абзацы собираются из шаблонов договоров со слотами; каждый слот — значение с категорией, поэтому
разметка точная. Все значения вымышленные; реквизиты с контрольными суммами генерируются.
Запуск отчёта: `python -m tests.redaction_corpus` (таблица точности и полноты по категориям).

Метрики:
- полнота (recall) — доля размеченных значений, которые целиком покрыты найденным вхождением той же
  группы категорий (частичное покрытие считается пропуском: значение ушло бы в облако частично);
- точность (precision) — доля найденных вхождений, пересекающихся с каким-либо размеченным значением.
"""

from __future__ import annotations

import random
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from app.redaction.categories import default_enabled
from app.redaction.scan import ScanConfig, scan

# Категории, которые считаются одним классом при оценке (различие — только контекст).
GROUPS = {
    "signatory": "person",
    "person_name": "person",
    "address_person": "address",
    "address_org": "address",
    "org_name": "org",
    "bank_name": "org",
}


def group(category: str) -> str:
    return GROUPS.get(category, category)


@dataclass(frozen=True)
class Label:
    fragment: str
    start: int
    end: int
    category: str


def _inn10(rng: random.Random) -> str:
    d = [rng.randint(0, 9) for _ in range(9)]
    d.append(sum(w * x for w, x in zip((2, 4, 10, 3, 5, 9, 4, 6, 8), d, strict=True)) % 11 % 10)
    return "".join(map(str, d))


def _inn12(rng: random.Random) -> str:
    d = [rng.randint(0, 9) for _ in range(10)]
    d.append(sum(w * x for w, x in zip((7, 2, 4, 10, 3, 5, 9, 4, 6, 8), d, strict=True)) % 11 % 10)
    d.append(sum(w * x for w, x in zip((3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8), d, strict=True)) % 11 % 10)
    return "".join(map(str, d))


def _ogrn(rng: random.Random) -> str:
    body = str(rng.choice((1, 5))) + "".join(str(rng.randint(0, 9)) for _ in range(11))
    return body + str(int(body) % 11 % 10)


def _snils(rng: random.Random) -> str:
    while True:
        d = [rng.randint(0, 9) for _ in range(9)]
        if int("".join(map(str, d))) > 1001998:
            break
    total = sum(x * (9 - i) for i, x in enumerate(d))
    total = total % 101 if total > 101 else total
    control = 0 if total in (100, 101) else total
    s = "".join(map(str, d))
    return f"{s[:3]}-{s[3:6]}-{s[6:9]} {control:02d}"


def _luhn_card(rng: random.Random) -> str:
    d = [4] + [rng.randint(0, 9) for _ in range(14)]
    total = 0
    for i, x in enumerate(reversed(d)):
        y = x * 2 if i % 2 == 0 else x
        total += y - 9 if y > 9 else y
    d.append((10 - total % 10) % 10)
    s = "".join(map(str, d))
    return " ".join(s[i : i + 4] for i in range(0, 16, 4))


# (именительный, родительный, дательный, инициалы «Фамилия И.О.» в именительном)
PEOPLE = [
    (
        "Смирнова Ольга Викторовна",
        "Смирновой Ольги Викторовны",
        "Смирновой Ольге Викторовне",
        "Смирнова О.В.",
    ),
    (
        "Кузнецов Алексей Дмитриевич",
        "Кузнецова Алексея Дмитриевича",
        "Кузнецову Алексею Дмитриевичу",
        "Кузнецов А.Д.",
    ),
    ("Попов Сергей Николаевич", "Попова Сергея Николаевича", "Попову Сергею Николаевичу", "Попов С.Н."),
    ("Волкова Мария Андреевна", "Волковой Марии Андреевны", "Волковой Марии Андреевне", "Волкова М.А."),
    ("Соколов Дмитрий Игоревич", "Соколова Дмитрия Игоревича", "Соколову Дмитрию Игоревичу", "Соколов Д.И."),
    (
        "Лебедева Екатерина Павловна",
        "Лебедевой Екатерины Павловны",
        "Лебедевой Екатерине Павловне",
        "Лебедева Е.П.",
    ),
    ("Новиков Павел Олегович", "Новикова Павла Олеговича", "Новикову Павлу Олеговичу", "Новиков П.О."),
    ("Морозова Анна Сергеевна", "Морозовой Анны Сергеевны", "Морозовой Анне Сергеевне", "Морозова А.С."),
]
ORGS = ["Северный ветер", "ТехноСфера", "Гранит-Строй", "Медиа Плюс", "Зелёная долина", "Альтаир", "Вектор"]
FORMS = ["ООО", "АО", "ПАО", "ЗАО", "Общество с ограниченной ответственностью", "Акционерное общество"]
CITIES = ["г. Москва", "г. Казань", "г. Екатеринбург", "г. Новосибирск", "г. Самара"]
STREETS = ["ул. Ленина", "ул. Садовая", "пр-т Мира", "ул. Гагарина", "пер. Тихий"]
BANKS = [("ПАО Сбербанк", "Сбербанк", "044525225", "30101810400000000225")]
DOMAINS = ["example.ru", "test-company.ru", "mail.example.org"]

NEGATIVE = [
    "Заказчик обязуется принять и оплатить услуги в течение 10 рабочих дней с даты подписания Акта.",
    "Исполнитель вправе привлекать третьих лиц, оставаясь ответственным перед Заказчиком за их действия.",
    "Споры разрешаются в Арбитражном суде по месту нахождения ответчика в соответствии с АПК РФ.",
    "Стороны руководствуются Гражданским кодексом Российской Федерации и Федеральным законом № 152-ФЗ.",
    "Поставщик гарантирует качество Товара в течение 12 месяцев с даты поставки.",
    "Покупатель обязан осмотреть Товар и сообщить Продавцу о недостатках в течение 5 дней.",
    "Арендатор не вправе сдавать Помещение в субаренду без письменного согласия Арендодателя.",
    "Настоящий Договор вступает в силу с момента подписания и действует до полного исполнения обязательств.",
    "Все приложения к Договору являются его неотъемлемой частью.",
    "Сторона, нарушившая обязательство, возмещает другой Стороне причинённые убытки в полном объёме.",
]


class _Builder:
    def __init__(self, key: str) -> None:
        self.key = key
        self.parts: list[str] = []
        self.pos = 0
        self.labels: list[Label] = []

    def text(self, value: str) -> _Builder:
        self.parts.append(value)
        self.pos += len(value)
        return self

    def slot(self, value: str, category: str) -> _Builder:
        self.labels.append(Label(self.key, self.pos, self.pos + len(value), category))
        return self.text(value)

    def build(self) -> tuple[str, str]:
        return self.key, "".join(self.parts)


Document = tuple[list[tuple[str, str]], list[Label]]


def corpus(seed: int = 20261021, documents: int = 12) -> list[Document]:
    """Синтетические договоры: каждый — (фрагменты, разметка); сканируются по отдельности."""
    rng = random.Random(seed)
    result: list[Document] = []
    fragments: list[tuple[str, str]] = []
    labels: list[Label] = []
    counter = 0

    def new() -> _Builder:
        nonlocal counter
        counter += 1
        return _Builder(f"F{counter:06d}")

    def add(b: _Builder) -> None:
        fragments.append(b.build())
        labels.extend(b.labels)

    for _ in range(documents):
        fragments, labels = [], []
        result.append((fragments, labels))
        person, signer = rng.sample(PEOPLE, 2)
        org_a, org_b = rng.sample(ORGS, 2)
        form_a, form_b = rng.choice(FORMS), rng.choice(FORMS)
        bank_full, bank_short, bik, corr = rng.choice(BANKS)
        city, street = rng.choice(CITIES), rng.choice(STREETS)
        index = f"{rng.randint(100000, 199999)}"
        house, flat = rng.randint(1, 120), rng.randint(1, 300)

        add(
            new()
            .text(f"{form_a} «")
            .slot(org_a, "org_name")
            .text("», в лице генерального директора ")
            .slot(signer[1], "signatory")
            .text(", действующего на основании Устава, именуемое в дальнейшем «Заказчик», и гражданин РФ ")
            .slot(person[0], "person_name")
            .text(", именуемый в дальнейшем «Исполнитель», заключили настоящий договор.")
        )
        add(new().text(NEGATIVE[rng.randrange(len(NEGATIVE))]))
        add(
            new()
            .text("Исполнитель: паспорт серия ")
            .slot(
                f"{rng.randint(10, 99)} {rng.randint(10, 99)} № {rng.randint(100000, 999999)}", "passport_ru"
            )
            .text(", выдан ")
            .slot(f"ГУ МВД России по {city.replace('г. ', 'г. ')}", "passport_ru")
            .text(" ")
            .slot(
                f"{rng.randint(10, 28):02d}.{rng.randint(1, 12):02d}.{rng.randint(2005, 2020)}", "passport_ru"
            )
            .text(", код подразделения ")
            .slot(f"{rng.randint(100, 999)}-{rng.randint(100, 999)}", "passport_ru")
            .text(", дата рождения ")
            .slot(
                f"{rng.randint(10, 28):02d}.{rng.randint(1, 12):02d}.{rng.randint(1960, 2000)}", "birth_date"
            )
            .text(", СНИЛС ")
            .slot(_snils(rng), "snils")
            .text(", ИНН ")
            .slot(_inn12(rng), "inn_person")
            .text(", зарегистрирован по адресу: ")
            .slot(f"{index}, {city}, {street}, д. {house}, кв. {flat}", "address_person")
            .text(".")
        )
        add(new().text(NEGATIVE[rng.randrange(len(NEGATIVE))]))
        email = f"{rng.choice(['info', 'office', 'zakaz', 'buh'])}@{rng.choice(DOMAINS)}"
        add(
            new()
            .text("Претензии направляются ")
            .slot(person[2], "person_name")
            .text(" по адресу электронной почты ")
            .slot(email, "email")
            .text(" или по телефону ")
            .slot(
                rng.choice(
                    [
                        f"+7 (9{rng.randint(10, 99)}) {rng.randint(100, 999)}"
                        f"-{rng.randint(10, 99)}-{rng.randint(10, 99)}",
                        f"8 (495) {rng.randint(100, 999)}-{rng.randint(10, 99)}-{rng.randint(10, 99)}",
                    ]
                ),
                "phone",
            )
            .text(".")
        )
        add(
            new()
            .text("Реквизиты Заказчика: ИНН ")
            .slot(_inn10(rng), "inn_org")
            .text(", КПП ")
            .slot(f"{rng.randint(1000, 9999)}01001", "kpp")
            .text(", ОГРН ")
            .slot(_ogrn(rng), "ogrn")
            .text(", юридический адрес: ")
            .slot(f"{index}, {city}, {street}, д. {house}", "address_org")
            .text("; р/с ")
            .slot("40702810" + "".join(str(rng.randint(0, 9)) for _ in range(12)), "bank_account")
            .text(" в ")
            .text(bank_full.replace(bank_short, ""))
            .slot(bank_short, "bank_name")
            .text(", БИК ")
            .slot(bik, "bik")
            .text(", к/с ")
            .slot(corr, "bank_account")
            .text(".")
        )
        add(
            new()
            .text("Оплата по карте ")
            .slot(_luhn_card(rng), "bank_card")
            .text(" допускается по согласованию Сторон. Договор с ")
            .text(f"{form_b} «")
            .slot(org_b, "org_name")
            .text("» действует до исполнения обязательств.")
        )
        # Трудные случаи: без вводных слов, без кавычек и форматирования (часть ожидаемо пропускается).
        other = rng.choice([p for p in PEOPLE if p not in (person, signer)])
        add(
            new()
            .text("Доверенность выдана на имя ")
            .slot(other[1], "person_name")
            .text(", который вправе подписывать акты. Ответственный за приёмку — ")
            .slot(other[3].split()[0] + " " + other[3].split()[1][:2], "person_name")
            .text(".")
        )
        add(
            new()
            .text("Работы выполняются по заказу компании ")
            .slot(rng.choice(ORGS), "org_name")
            .text(". Доставка на склад: ")
            .slot(f"{rng.choice(CITIES)}, {rng.choice(STREETS)}, д. {rng.randint(1, 90)}", "address_org")
            .text(". Контактный номер ")
            .slot(f"89{rng.randint(100000000, 999999999)}", "phone")
            .text(".")
        )
        add(
            new().text(
                "Приложение № 1 «Техническое задание» и Приложение № 2 «Календарный план» прилагаются."
            )
        )
        add(
            new()
            .text("Заказчик ____________ /")
            .slot(signer[3], "signatory")
            .text("/        Исполнитель ____________ /")
            .slot(person[3], "person_name")
            .text("/")
        )
    return result


@dataclass
class Metrics:
    expected: Counter[str]
    covered: Counter[str]
    detected: Counter[str]
    correct: Counter[str]

    def rows(self) -> Iterable[tuple[str, int, int, float, int, float]]:
        for category in sorted(set(self.expected) | set(self.detected)):
            expected, covered = self.expected[category], self.covered[category]
            detected, correct = self.detected[category], self.correct[category]
            yield (
                category,
                expected,
                covered,
                covered / expected if expected else float("nan"),
                detected,
                correct / detected if detected else float("nan"),
            )


def evaluate(enabled: frozenset[str] | None = None) -> Metrics:
    """По умолчанию — категории, включённые по умолчанию (то, что получает пользователь)."""
    config = ScanConfig(enabled=enabled or frozenset(c for c, on in default_enabled().items() if on))
    m = Metrics(Counter(), Counter(), Counter(), Counter())
    for fragments, labels in corpus():
        result = scan(fragments, config)
        found = [
            (o["fragment"], o["start"], o["end"], group(e["category"]))
            for e in result["entities"]
            for o in e["occurrences"]
        ]
        for label in labels:
            g = group(label.category)
            m.expected[g] += 1
            if any(
                f == label.fragment and s <= label.start and label.end <= e and c == g for f, s, e, c in found
            ):
                m.covered[g] += 1
        for f, s, e, c in found:
            m.detected[c] += 1
            if any(lb.fragment == f and lb.start < e and s < lb.end for lb in labels):
                m.correct[c] += 1
    return m


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    m = evaluate()
    print("| Категория | Размечено | Найдено целиком | Полнота | Найдено вхождений | Точность |")
    print("|---|---|---|---|---|---|")
    for category, expected, covered, recall, detected, precision in m.rows():
        r = f"{recall:.3f}" if expected else "—"
        p = f"{precision:.3f}" if detected else "—"
        print(f"| {category} | {expected} | {covered} | {r} | {detected} | {p} |")
    total_e, total_c = sum(m.expected.values()), sum(m.covered.values())
    total_d, total_ok = sum(m.detected.values()), sum(m.correct.values())
    print(
        f"| **всего** | {total_e} | {total_c} | {total_c / total_e:.3f} | {total_d} "
        f"| {total_ok / total_d:.3f} |"
    )


if __name__ == "__main__":
    main()
