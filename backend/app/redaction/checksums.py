"""Контрольные суммы реквизитов: ИНН, ОГРН/ОГРНИП, СНИЛС, ОКПО, ключ счёта с БИК, Луна, IBAN, VIN.

Все функции принимают строку только из цифр (VIN, IBAN — из букв и цифр) и не бросают исключений.
"""

from __future__ import annotations


def _digits(value: str) -> list[int] | None:
    return [int(c) for c in value] if value.isascii() and value.isdigit() else None


def inn_valid(value: str) -> bool:
    d = _digits(value)
    if d is None:
        return False

    def check(weights: tuple[int, ...]) -> int:
        return sum(w * x for w, x in zip(weights, d, strict=False)) % 11 % 10

    if len(d) == 10:
        return check((2, 4, 10, 3, 5, 9, 4, 6, 8)) == d[9]
    if len(d) == 12:
        return (
            check((7, 2, 4, 10, 3, 5, 9, 4, 6, 8)) == d[10]
            and check((3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)) == d[11]
        )
    return False


def ogrn_valid(value: str) -> bool:
    """ОГРН (13 цифр, остаток от деления на 11) и ОГРНИП (15 цифр, на 13)."""
    d = _digits(value)
    if d is None or len(d) not in (13, 15):
        return False
    body = int(value[:-1])
    mod = 11 if len(d) == 13 else 13
    return body % mod % 10 == d[-1]


def snils_valid(value: str) -> bool:
    d = _digits(value)
    if d is None or len(d) != 11:
        return False
    number = int(value[:9])
    if number <= 1001998:
        # Номера до 001-001-998 контрольной суммой не проверяются.
        return True
    total = sum(x * (9 - i) for i, x in enumerate(d[:9]))
    if total > 101:
        total %= 101
    control = 0 if total in (100, 101) else total
    return control == int(value[9:])


def okpo_valid(value: str) -> bool:
    d = _digits(value)
    if d is None or len(d) not in (8, 10):
        return False
    body, control = d[:-1], d[-1]
    for shift in (0, 2):
        total = sum(x * ((i + shift) % 10 + 1) for i, x in enumerate(body)) % 11
        if total < 10:
            return total == control
    return control == 0


def account_key_valid(account: str, bik: str) -> bool:
    """Ключ счёта (20 цифр) по БИК: расчётный — последние 3 цифры БИК, корсчёт — «0» + 5–6 цифры БИК."""
    a, b = _digits(account), _digits(bik)
    if a is None or b is None or len(a) != 20 or len(b) != 9:
        return False
    weights = (7, 1, 3) * 8
    for prefix in (bik[-3:], "0" + bik[4:6]):
        digits = [int(c) for c in prefix + account]
        if sum(w * x % 10 for w, x in zip(weights, digits, strict=False)) % 10 == 0:
            return True
    return False


def luhn_valid(value: str) -> bool:
    d = _digits(value)
    if d is None or len(d) < 12:
        return False
    total = 0
    for i, x in enumerate(reversed(d)):
        y = x * 2 if i % 2 == 1 else x
        total += y - 9 if y > 9 else y
    return total % 10 == 0


def iban_valid(value: str) -> bool:
    v = value.upper()
    if not (15 <= len(v) <= 34) or not v.isascii() or not v.isalnum():
        return False
    rearranged = v[4:] + v[:4]
    numeric = "".join(str(int(c, 36)) for c in rearranged)
    return int(numeric) % 97 == 1


_VIN_VALUES = {
    **{str(i): i for i in range(10)},
    **dict(zip("ABCDEFGH", range(1, 9), strict=True)),
    **dict(zip("JKLMN", range(1, 6), strict=True)),
    "P": 7,
    "R": 9,
    **dict(zip("STUVWXYZ", range(2, 10), strict=True)),
}
_VIN_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)


def vin_check_digit_valid(value: str) -> bool:
    """Контрольная цифра VIN (9-й символ, стандарт Северной Америки; у многих VIN других рынков её нет)."""
    v = value.upper()
    if len(v) != 17 or any(c not in _VIN_VALUES for c in v):
        return False
    total = sum(_VIN_VALUES[c] * w for c, w in zip(v, _VIN_WEIGHTS, strict=True)) % 11
    return v[8] == ("X" if total == 10 else str(total))
