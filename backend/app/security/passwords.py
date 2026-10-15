"""Хеширование паролей (Argon2id) и политика сложности.

Пароли и их хеши никогда не логируются. Ошибки политики описывают нарушенное правило,
но не повторяют сам пароль.
"""

from __future__ import annotations

import secrets
import string
from dataclasses import dataclass
from functools import lru_cache

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD_LENGTH = 12
# Верхняя граница защищает от DoS через очень длинные пароли (Argon2 обрабатывает их целиком).
MAX_PASSWORD_LENGTH = 128

# Небольшой встроенный список самых распространённых паролей и шаблонов (сравнение без регистра).
# Пароли короче 12 символов отсекаются длиной, поэтому здесь — длинные распространённые варианты.
_COMMON_PASSWORDS = frozenset(
    {
        "123456789012",
        "1234567890123",
        "12345678901234",
        "123456123456",
        "111111111111",
        "000000000000",
        "qwertyuiopas",
        "qwerty123456",
        "123456qwerty",
        "qwertyqwerty",
        "1q2w3e4r5t6y",
        "1qaz2wsx3edc",
        "zaq12wsxcde3",
        "password1234",
        "password12345",
        "password123!",
        "passwordpassword",
        "administrator",
        "administrator1",
        "admin1234567",
        "adminadmin123",
        "iloveyou1234",
        "letmein12345",
        "welcome12345",
        "changeme1234",
        "superadmin123",
        "superadmin1234",
        "пароль123456",
        "йцукенгшщзхъ",
        "qwertyuiop123",
        "abcdefghijkl",
        "abc123abc123",
    }
)


class PasswordPolicyError(ValueError):
    """Пароль не соответствует политике. `code` — машинный код, `message` — для пользователя."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


def validate_password(password: str, *, email: str | None = None, display_name: str | None = None) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            "password_too_short", f"Пароль должен содержать не менее {MIN_PASSWORD_LENGTH} символов"
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            "password_too_long", f"Пароль должен содержать не более {MAX_PASSWORD_LENGTH} символов"
        )
    if not password.strip() or len(set(password)) < 4:
        raise PasswordPolicyError("password_too_simple", "Пароль слишком простой")
    lowered = password.lower()
    if lowered in _COMMON_PASSWORDS:
        raise PasswordPolicyError("password_too_common", "Пароль входит в список распространённых")
    for personal in (email, email.split("@", 1)[0] if email else None, display_name):
        if personal and len(personal) >= 4 and personal.lower() in lowered:
            raise PasswordPolicyError(
                "password_contains_personal_data", "Пароль не должен содержать e-mail или имя пользователя"
            )


@dataclass(frozen=True)
class HashParams:
    time_cost: int
    memory_cost: int  # КиБ
    parallelism: int


# RFC 9106, рекомендуемый профиль для ограниченной памяти (значения argon2-cffi по умолчанию):
# t=3, m=64 МиБ, p=4. Выше минимальных рекомендаций OWASP (m=19 МиБ, t=2, p=1).
PRODUCTION_PARAMS = HashParams(time_cost=3, memory_cost=64 * 1024, parallelism=4)
# Только для ENV=test: ускоряет тесты; в production не используется.
TEST_PARAMS = HashParams(time_cost=1, memory_cost=8 * 1024, parallelism=1)


@lru_cache(maxsize=2)
def _hasher(params: HashParams) -> PasswordHasher:
    return PasswordHasher(
        time_cost=params.time_cost,
        memory_cost=params.memory_cost,
        parallelism=params.parallelism,
        hash_len=32,
        salt_len=16,
        type=Type.ID,
    )


class PasswordService:
    def __init__(self, params: HashParams = PRODUCTION_PARAMS) -> None:
        self._ph = _hasher(params)
        # Хеш для выравнивания времени ответа, когда пользователь не найден.
        self._dummy_hash = self._ph.hash(secrets.token_urlsafe(16))

    def hash(self, password: str) -> str:
        return self._ph.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        try:
            return self._ph.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def verify_dummy(self, password: str) -> None:
        """Тратит то же время, что и проверка настоящего хеша (защита от перебора e-mail по времени)."""
        self.verify(self._dummy_hash, password)

    def needs_rehash(self, password_hash: str) -> bool:
        try:
            return self._ph.check_needs_rehash(password_hash)
        except InvalidHashError:
            return True


_TEMP_ALPHABET = "".join(c for c in string.ascii_letters + string.digits if c not in "Il1O0o")


def generate_temporary_password(length: int = 16) -> str:
    """Одноразовый временный пароль (≈ 95 бит энтропии при длине 16); пользователь обязан его сменить."""
    while True:
        candidate = "".join(secrets.choice(_TEMP_ALPHABET) for _ in range(length))
        if (
            any(c.islower() for c in candidate)
            and any(c.isupper() for c in candidate)
            and any(c.isdigit() for c in candidate)
        ):
            return candidate
