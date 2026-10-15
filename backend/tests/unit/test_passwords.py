"""Argon2id и политика паролей."""

from __future__ import annotations

import pytest

from app.security.passwords import (
    PRODUCTION_PARAMS,
    TEST_PARAMS,
    PasswordPolicyError,
    PasswordService,
    generate_temporary_password,
    validate_password,
)


@pytest.fixture(scope="module")
def service() -> PasswordService:
    return PasswordService(TEST_PARAMS)


def test_hash_is_argon2id_and_salted(service: PasswordService) -> None:
    h1 = service.hash("Correct-Horse-Battery-9")
    h2 = service.hash("Correct-Horse-Battery-9")
    assert h1.startswith("$argon2id$")
    assert h1 != h2  # разная соль
    assert "Correct-Horse" not in h1


def test_verify(service: PasswordService) -> None:
    h = service.hash("Correct-Horse-Battery-9")
    assert service.verify(h, "Correct-Horse-Battery-9")
    assert not service.verify(h, "correct-horse-battery-9")
    assert not service.verify("not-a-hash", "Correct-Horse-Battery-9")


def test_production_params_meet_owasp_minimum() -> None:
    # OWASP Password Storage Cheat Sheet: Argon2id, m ≥ 19 МиБ, t ≥ 2, p ≥ 1.
    assert PRODUCTION_PARAMS.memory_cost >= 19 * 1024
    assert PRODUCTION_PARAMS.time_cost >= 2


def test_needs_rehash_when_params_change(service: PasswordService) -> None:
    weak_hash = service.hash("Correct-Horse-Battery-9")
    assert not service.needs_rehash(weak_hash)
    assert PasswordService(PRODUCTION_PARAMS).needs_rehash(weak_hash)


@pytest.mark.parametrize(
    ("password", "code"),
    [
        ("short-1A", "password_too_short"),
        ("x" * 129, "password_too_long"),
        ("aaaaaaaaaaaaaa", "password_too_simple"),
        ("            ", "password_too_simple"),
        ("Password1234", "password_too_common"),
        ("ivan.petrov-2026!", "password_contains_personal_data"),
    ],
)
def test_policy_rejects(password: str, code: str) -> None:
    with pytest.raises(PasswordPolicyError) as exc:
        validate_password(password, email="ivan.petrov@example.org", display_name="Иван Петров")
    assert exc.value.code == code
    assert password.strip() == "" or password not in exc.value.message


def test_policy_accepts_strong_password() -> None:
    validate_password("Tiger-Lamp-Orbit-42", email="ivan.petrov@example.org", display_name="Иван Петров")


def test_temporary_password_passes_policy_and_is_random() -> None:
    values = {generate_temporary_password() for _ in range(50)}
    assert len(values) == 50
    for value in values:
        assert len(value) == 16
        validate_password(value)
