"""Проверка Origin, токены, конфигурация bootstrap — без БД."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.requests import Request

from app.api.deps import origin_allowed
from app.config import Settings
from app.security.tokens import keyed_hash, new_token
from tests.conftest import make_settings


def _request(method: str, headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "method": method,
        "path": "/api/x",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


@pytest.mark.parametrize(
    ("headers", "allowed"),
    [
        ({"Origin": "http://localhost:8080"}, True),
        ({"Origin": "http://evil.example"}, False),
        ({"Origin": "null"}, False),
        ({"Referer": "http://localhost:8080/admin/users"}, True),
        ({"Referer": "http://localhost:8080.evil.example/x"}, False),
        ({}, False),
        # Origin имеет приоритет над Referer
        ({"Origin": "http://evil.example", "Referer": "http://localhost:8080/"}, False),
    ],
)
def test_origin_allowed(settings: Settings, headers: dict[str, str], allowed: bool) -> None:
    assert origin_allowed(_request("POST", headers), settings) is allowed


def test_tokens_are_random_and_hashes_keyed() -> None:
    t1, t2 = new_token(), new_token()
    assert t1 != t2
    assert len(t1) >= 43  # 256 бит в base64url
    assert keyed_hash("k1" * 16, "session", t1) != keyed_hash("k2" * 16, "session", t1)
    assert keyed_hash("k1" * 16, "session", t1) != keyed_hash("k1" * 16, "csrf", t1)
    assert len(keyed_hash("k1" * 16, "session", t1)) == 64


def test_bootstrap_empty_values_are_none(tmp_path: Path) -> None:
    s = make_settings(
        tmp_path, bootstrap_admin_email="  ", bootstrap_admin_password="", bootstrap_admin_name=""
    )
    assert s.bootstrap_admin_email is None
    assert s.bootstrap_admin_password is None
    assert s.bootstrap_admin_name is None
    assert s.bootstrap_enabled is True


def test_bootstrap_password_hidden_in_repr(tmp_path: Path) -> None:
    s = make_settings(tmp_path, bootstrap_admin_password="Very-Secret-Bootstrap-77")
    assert "Very-Secret-Bootstrap-77" not in repr(s)
    assert "Very-Secret-Bootstrap-77" not in str(s.model_dump())
