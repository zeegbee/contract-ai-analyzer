"""Случайные токены и их хеши.

В БД хранится только HMAC-SHA256(SECRET_KEY, token): утечка таблицы сессий не даёт рабочих токенов,
а ротация SECRET_KEY инвалидирует все сессии.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

TOKEN_BYTES = 32  # 256 бит


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def keyed_hash(secret_key: str, purpose: str, value: str) -> str:
    """HMAC-SHA256 с разделением назначений (`purpose`), hex — 64 символа."""
    message = f"{purpose}\x00{value}".encode()
    return hmac.new(secret_key.encode(), message, hashlib.sha256).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
