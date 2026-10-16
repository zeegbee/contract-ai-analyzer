"""Шифрование секретов провайдеров at rest (MultiFernet).

Ключи — `SECRETS_ENCRYPTION_KEYS` в окружении/secret manager, отдельно от БД и бэкапов. Первый ключ
шифрует, остальные — только расшифровывают (ротация: новый ключ первым → `python -m app.cli
rotate-secrets` → старый ключ удаляется из списка). В БД хранится идентификатор ключа (отпечаток),
но не сам ключ.
"""

from __future__ import annotations

import hashlib

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from pydantic import SecretStr

from app.errors import AppError


class SecretsNotConfigured(AppError):
    status_code = 503
    code = "secrets_encryption_not_configured"
    message = "Шифрование секретов не настроено (SECRETS_ENCRYPTION_KEYS)"


class SecretUndecryptable(AppError):
    status_code = 409
    code = "secret_undecryptable"
    message = "Сохранённый ключ провайдера не расшифровывается текущими ключами; введите ключ заново"


def key_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


class SecretBox:
    def __init__(self, keys: list[SecretStr]) -> None:
        raw = [k.get_secret_value() for k in keys]
        self._fernet = MultiFernet([Fernet(k.encode()) for k in raw]) if raw else None
        self.primary_key_id = key_fingerprint(raw[0]) if raw else None

    def __repr__(self) -> str:
        return f"SecretBox(primary_key_id={self.primary_key_id!r})"

    @property
    def configured(self) -> bool:
        return self._fernet is not None

    def encrypt(self, plaintext: str) -> tuple[bytes, str]:
        if self._fernet is None or self.primary_key_id is None:
            raise SecretsNotConfigured()
        return self._fernet.encrypt(plaintext.encode()), self.primary_key_id

    def decrypt(self, ciphertext: bytes) -> str:
        if self._fernet is None:
            raise SecretsNotConfigured()
        try:
            return self._fernet.decrypt(ciphertext).decode()
        except InvalidToken:
            raise SecretUndecryptable() from None

    def rotate(self, ciphertext: bytes) -> tuple[bytes, str]:
        """Перешифровывает основным ключом."""
        if self._fernet is None or self.primary_key_id is None:
            raise SecretsNotConfigured()
        try:
            return self._fernet.rotate(ciphertext), self.primary_key_id
        except InvalidToken:
            raise SecretUndecryptable() from None
