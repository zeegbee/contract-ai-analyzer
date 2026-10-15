"""Типизированная конфигурация приложения.

Все значения читаются из переменных окружения (или `.env` в режиме разработки).
Ошибки конфигурации останавливают запуск процесса — сервер не стартует с небезопасными
или неполными настройками.
"""

from __future__ import annotations

import base64
import binascii
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Значения из .env.example, которые нельзя использовать вне разработки.
_PLACEHOLDER_MARKERS = ("change-me", "changeme", "example", "placeholder")


class Environment(StrEnum):
    development = "development"
    test = "test"
    production = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    env: Environment = Environment.production
    app_version: str = "0.1.0"

    # --- База данных ---
    database_url: SecretStr
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_statement_timeout_ms: int = Field(default=30_000, ge=1_000)

    # --- Секреты ---
    secret_key: SecretStr = Field(description="Ключ для HMAC (CSRF и т.п.), ≥ 32 символов")
    # Список ключей Fernet через запятую; первый используется для шифрования, остальные — для
    # расшифровки при ротации. Обязателен в production.
    secrets_encryption_keys: Annotated[list[SecretStr], NoDecode] = Field(default_factory=list)

    # --- HTTP ---
    allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    cookie_secure: bool = True
    trusted_proxy_ips: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["127.0.0.1"])

    # --- Хранилище ---
    storage_dir: Path = Path("/data/storage")

    # --- Логи ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"

    # --- Worker / очередь ---
    worker_concurrency: int = Field(default=2, ge=1, le=32)
    job_lease_seconds: int = Field(default=120, ge=15, le=3600)
    job_poll_interval_seconds: float = Field(default=2.0, gt=0, le=60)
    worker_heartbeat_file: Path = Path("/tmp/worker-heartbeat")  # noqa: S108 - файл внутри контейнера

    # --- Сервисы ---
    converter_url: str = "http://converter:8090"

    # --- Сессии и вход ---
    session_idle_minutes: int = Field(default=480, ge=5, le=7 * 24 * 60)
    session_absolute_hours: int = Field(default=168, ge=1, le=90 * 24)
    # Rate limit входа: неудачные попытки в скользящем окне.
    login_window_minutes: int = Field(default=15, ge=1, le=24 * 60)
    login_max_failures_per_email: int = Field(default=5, ge=1, le=100)
    login_max_failures_per_ip: int = Field(default=30, ge=1, le=10_000)
    # Прогрессивная задержка ответа после N-й неудачи по email (0 — выключена).
    login_delay_step_ms: int = Field(default=500, ge=0, le=5_000)
    login_delay_max_ms: int = Field(default=3_000, ge=0, le=30_000)

    # --- Bootstrap первого Superadmin ---
    # После инициализации рекомендуется false и удалить BOOTSTRAP_ADMIN_* из окружения.
    bootstrap_enabled: bool = True
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: SecretStr | None = None
    bootstrap_admin_name: str | None = None

    @field_validator("secrets_encryption_keys", mode="before")
    @classmethod
    def _split_keys(cls, value: object) -> object:
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return value

    @field_validator("allowed_origins", "trusted_proxy_ips", mode="before")
    @classmethod
    def _split_list(cls, value: object) -> object:
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return value

    @field_validator(
        "bootstrap_admin_email", "bootstrap_admin_password", "bootstrap_admin_name", mode="before"
    )
    @classmethod
    def _empty_as_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: SecretStr) -> SecretStr:
        url = value.get_secret_value()
        if not url.startswith("postgresql+psycopg://"):
            raise ValueError("DATABASE_URL должен начинаться с postgresql+psycopg://")
        return value

    @field_validator("secret_key")
    @classmethod
    def _check_secret_key(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("SECRET_KEY должен содержать не менее 32 символов")
        return value

    @field_validator("secrets_encryption_keys")
    @classmethod
    def _check_fernet_keys(cls, value: list[SecretStr]) -> list[SecretStr]:
        for key in value:
            try:
                raw = base64.urlsafe_b64decode(key.get_secret_value().encode())
            except (binascii.Error, ValueError) as exc:
                raise ValueError("SECRETS_ENCRYPTION_KEYS: ключ не в формате Fernet (base64)") from exc
            if len(raw) != 32:
                raise ValueError("SECRETS_ENCRYPTION_KEYS: ключ Fernet должен быть 32 байта в base64")
        return value

    @field_validator("allowed_origins")
    @classmethod
    def _check_origins(cls, value: list[str]) -> list[str]:
        for origin in value:
            if not origin.startswith(("http://", "https://")) or origin.endswith("/"):
                raise ValueError(f"ALLOWED_ORIGINS: некорректный origin {origin!r}")
        return value

    @model_validator(mode="after")
    def _production_rules(self) -> Settings:
        if self.env is not Environment.production:
            return self
        problems: list[str] = []
        if not self.cookie_secure:
            problems.append("COOKIE_SECURE должен быть true")
        if not self.secrets_encryption_keys:
            problems.append("SECRETS_ENCRYPTION_KEYS обязателен")
        if not self.allowed_origins:
            problems.append("ALLOWED_ORIGINS обязателен")
        elif any(o.startswith("http://") for o in self.allowed_origins):
            problems.append("ALLOWED_ORIGINS должен использовать https")
        secret = self.secret_key.get_secret_value().lower()
        if any(marker in secret for marker in _PLACEHOLDER_MARKERS):
            problems.append("SECRET_KEY содержит значение-заглушку")
        if self.log_level == "DEBUG":
            problems.append("LOG_LEVEL=DEBUG запрещён")
        if problems:
            raise ValueError("Небезопасная production-конфигурация: " + "; ".join(problems))
        return self

    @property
    def is_production(self) -> bool:
        return self.env is Environment.production

    @property
    def is_test(self) -> bool:
        return self.env is Environment.test

    @property
    def docs_enabled(self) -> bool:
        return self.env is Environment.development


class ConfigurationError(RuntimeError):
    """Ошибка конфигурации без значений переменных окружения (в них могут быть секреты)."""


def safe_validation_message(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors(include_input=False, include_url=False, include_context=False):
        loc = ".".join(str(p) for p in err["loc"]).upper() or "CONFIG"
        parts.append(f"{loc}: {err['msg']}")
    return "Некорректная конфигурация: " + "; ".join(parts)


def load_settings() -> Settings:
    try:
        return Settings()  # значения из окружения
    except ValidationError as exc:
        # `from None`: исходное исключение pydantic содержит input_value (сырые значения окружения).
        raise ConfigurationError(safe_validation_message(exc)) from None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
