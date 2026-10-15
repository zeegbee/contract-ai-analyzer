from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Environment
from tests.conftest import make_settings


def test_development_settings_are_valid(tmp_path: Path) -> None:
    s = make_settings(tmp_path, env=Environment.development)
    assert s.docs_enabled
    assert not s.is_production


def test_production_requires_secure_configuration(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as exc:
        make_settings(
            tmp_path,
            env=Environment.production,
            cookie_secure=False,
            secrets_encryption_keys=[],
            allowed_origins=["http://example.org"],
        )
    message = str(exc.value)
    assert "COOKIE_SECURE" in message
    assert "SECRETS_ENCRYPTION_KEYS" in message
    assert "https" in message


def test_production_rejects_placeholder_secret(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="заглушку"):
        make_settings(
            tmp_path,
            env=Environment.production,
            cookie_secure=True,
            allowed_origins=["https://contracts.example.org"],
            secret_key="change-me-change-me-change-me-change-me",
        )


def test_production_valid(tmp_path: Path) -> None:
    s = make_settings(
        tmp_path,
        env=Environment.production,
        cookie_secure=True,
        allowed_origins=["https://contracts.local"],
    )
    assert s.is_production
    assert not s.docs_enabled


def test_short_secret_key_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        make_settings(tmp_path, secret_key="short")


def test_invalid_fernet_key_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="Fernet"):
        make_settings(tmp_path, secrets_encryption_keys=["not-a-key"])


def test_database_url_must_use_psycopg(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="postgresql\\+psycopg"):
        make_settings(tmp_path, database_url="postgresql://u:p@h/db")


def test_comma_separated_env_lists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cryptography.fernet import Fernet

    from app.config import Settings

    k1, k2 = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEYS", f"{k1}, {k2}")
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost:8080,https://a.example")
    s = Settings(_env_file=None)
    assert [k.get_secret_value() for k in s.secrets_encryption_keys] == [k1, k2]
    assert s.allowed_origins == ["http://localhost:8080", "https://a.example"]


def test_secrets_not_in_repr(tmp_path: Path) -> None:
    s = make_settings(tmp_path)
    text = repr(s) + str(s.model_dump())
    assert "test-secret-key" not in text
    assert ":pw@" not in text


def test_configuration_error_does_not_leak_values(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import ConfigurationError, load_settings

    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://cai:TOPSECRETPW@db/cai")
    monkeypatch.setenv("SECRET_KEY", "VERYSECRETKEY-" + "z" * 40)
    monkeypatch.setenv("COOKIE_SECURE", "false")
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost")
    with pytest.raises(ConfigurationError) as exc:
        load_settings()
    text = str(exc.value) + repr(exc.value)
    assert "COOKIE_SECURE" in text
    assert "TOPSECRETPW" not in text
    assert "VERYSECRETKEY" not in text
    assert exc.value.__cause__ is None and exc.value.__suppress_context__
