from __future__ import annotations

import json
import logging

import pytest

from app.logging import REDACTED, configure_logging, get_logger, redact_value

CONTRACT_TEXT = "Арендатор обязуется уплатить неустойку в размере 0,5% за каждый день просрочки. " * 20


def test_secret_keys_are_redacted() -> None:
    data = {
        "password": "hunter2-secret",
        "api_key": "sk-or-v1-abcdef",
        "Authorization": "Bearer abc",
        "nested": {"session_token": "tok", "ok": 1},
        "database_url": "postgresql+psycopg://u:p@h/db",
    }
    out = redact_value(None, data)
    assert out["password"] == REDACTED
    assert out["api_key"] == REDACTED
    assert out["Authorization"] == REDACTED
    assert out["nested"]["session_token"] == REDACTED
    assert out["nested"]["ok"] == 1
    assert out["database_url"] == REDACTED


def test_content_keys_are_replaced_with_length() -> None:
    assert redact_value("text", CONTRACT_TEXT) == f"[CONTENT len={len(CONTRACT_TEXT)}]"
    assert redact_value("quote", "цитата") == "[CONTENT len=6]"


def test_secret_like_values_masked_anywhere() -> None:
    msg = "call failed: Authorization: Bearer abcdefghijklmnop key sk-or-v1-0123456789abcdefgh"
    out = redact_value("detail", msg)
    assert "abcdefghijklmnop" not in out
    assert "0123456789abcdefgh" not in out


def test_dsn_password_masked_in_free_text() -> None:
    out = redact_value("error", "could not connect postgresql+psycopg://cai:s3cr3t@db:5432/cai")
    assert "s3cr3t" not in out
    assert "cai:" in out


def test_long_strings_truncated() -> None:
    out = redact_value("detail", "x" * 1000)
    assert len(out) < 400
    assert "truncated" in out


def test_end_to_end_log_output_contains_no_secrets(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")
    log = get_logger("test")
    log.info(
        "provider_call",
        api_key="sk-or-v1-supersecretvalue123456",
        password="P@ssw0rd-very-secret",
        text=CONTRACT_TEXT,
        detail=f"failed for {CONTRACT_TEXT}",
    )
    logging.getLogger("stdlib").warning("stdlib message with Bearer qwertyuiopasdfgh")
    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.strip().splitlines()]
    assert lines[0]["event"] == "provider_call"
    assert "supersecretvalue" not in out
    assert "P@ssw0rd" not in out
    assert "qwertyuiopasdfgh" not in out
    # полный текст договора в лог не попал: поле `text` заменено длиной, свободное поле обрезано
    assert CONTRACT_TEXT not in out
    assert lines[0]["text"] == f"[CONTENT len={len(CONTRACT_TEXT)}]"
    assert len(lines[0]["detail"]) < 400


VICTIM_EMAIL = "victim@" + "example.org"


def _raise_with_contract_text() -> None:
    # Строки сообщений собираются из переменных: в трассировке видна строка кода, но не значения.
    inner, outer = f"input_value='{CONTRACT_TEXT}'", f"DETAIL: Key (email)=({VICTIM_EMAIL}) already exists"
    try:
        raise ValueError(inner)
    except ValueError as exc:
        raise RuntimeError(outer) from exc


def test_exception_messages_are_not_logged(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")
    try:
        _raise_with_contract_text()
    except RuntimeError as exc:
        get_logger("test").error("job_crashed", exc_type=type(exc).__name__, exc_info=exc)
        # Сторонние библиотеки логируют через stdlib с exc_info=True.
        logging.getLogger("uvicorn.error").exception("Exception in ASGI application")
    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.strip().splitlines()]
    assert len(lines) == 2
    assert "неустойку" not in out
    assert VICTIM_EMAIL not in out
    for line in lines:
        trace = line["exception"]
        # Типы и кадры стека для диагностики остаются, сообщения — нет.
        assert "builtins.ValueError: [message hidden]" in trace
        assert "builtins.RuntimeError: [message hidden]" in trace
        assert "_raise_with_contract_text" in trace
        assert trace.index("ValueError") < trace.index("RuntimeError")
