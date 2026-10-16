"""Дочерний процесс разбора и клиент конвертера."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from app.ingestion import sandbox
from app.ingestion.converter_client import ConverterClient, ConverterUnavailable
from app.ingestion.model import ExtractionFailed, NoTextLayer, RawExtraction
from app.ingestion.sandbox import SandboxError, SandboxLimits, run_sandbox
from tests import docfactory as f

LIMITS = SandboxLimits(timeout_seconds=60, memory_mb=1024, max_pages=500)


def test_sandbox_extracts_docx_and_pdf(tmp_path: Path) -> None:
    docx = RawExtraction.from_json(run_sandbox("extract-docx", f.contract_docx(tmp_path / "c.docx"), LIMITS))
    assert f.CONTRACT_PARAGRAPHS[0] in [fr.text for fr in docx.fragments]
    pdf = RawExtraction.from_json(
        run_sandbox(
            "extract-pdf", f.text_pdf(tmp_path / "t.pdf", [["Страница один"], ["Страница два"]]), LIMITS
        )
    )
    assert [fr.page_start for fr in pdf.fragments] == [1, 2]
    pages = run_sandbox("pdf-page-texts", tmp_path / "t.pdf", LIMITS)["pages"]
    assert [p.strip() for p in pages] == ["Страница один", "Страница два"]


def test_sandbox_reports_failures_as_codes(tmp_path: Path) -> None:
    with pytest.raises(NoTextLayer):
        run_sandbox("extract-pdf", f.scanned_pdf(tmp_path / "s.pdf"), LIMITS)
    with pytest.raises(ExtractionFailed) as exc:
        run_sandbox("extract-pdf", f.encrypted_pdf(tmp_path / "e.pdf"), LIMITS)
    assert exc.value.code == "encrypted_document"
    with pytest.raises(ExtractionFailed) as exc:
        run_sandbox("extract-docx", f.zip_bomb_docx(tmp_path / "b.docx"), LIMITS)
    assert exc.value.code == "archive_limits_exceeded"


def test_sandbox_timeout_kills_child(tmp_path: Path) -> None:
    path = f.text_pdf(tmp_path / "t.pdf", [["Текст"]])
    with pytest.raises(SandboxError) as exc:
        run_sandbox("extract-pdf", path, SandboxLimits(timeout_seconds=0, memory_mb=1024, max_pages=10))
    assert exc.value.code == "extraction_timeout"


def test_child_environment_has_no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", "super-secret-value")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db/x")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-xxx")
    env = sandbox._child_env()
    assert not {"SECRET_KEY", "DATABASE_URL", "OPENROUTER_API_KEY"} & set(env)
    assert "super-secret-value" not in json.dumps(env)
    if os.name == "posix":
        assert set(env) == {"PATH", "LANG"}


def test_child_main_emits_single_result_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = f.text_pdf(tmp_path / "t.pdf", [["Текст договора поставки"]])
    args = ["extract-pdf", str(path), "--max-pages", "5", "--memory-mb", "1024", "--cpu-seconds", "30"]
    # Лимиты RLIMIT_* в процессе тестов не ставим.
    monkeypatch.setattr(sandbox, "_apply_limits", lambda *_: None)
    assert sandbox.child_main(args) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith(sandbox.RESULT_PREFIX)
    assert json.loads(lines[0][len(sandbox.RESULT_PREFIX) :])["ok"] is True


# ---------------------------------------------------------------- converter


def _client(handler: httpx.MockTransport) -> ConverterClient:
    return ConverterClient("http://converter:8090", 10, transport=handler)


async def test_converter_success(tmp_path: Path) -> None:
    seen: dict[str, str] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        assert request.content == b"source-bytes"
        return httpx.Response(200, content=b"PK\x03\x04converted")

    src = f.write(tmp_path / "in.doc", b"source-bytes")
    size = await _client(httpx.MockTransport(handle)).convert(src, "doc", "docx", tmp_path / "out.docx")
    assert size == len(b"PK\x03\x04converted") and (tmp_path / "out.docx").read_bytes().startswith(b"PK")
    assert seen == {"from": "doc", "to": "docx"}


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (503, {"error": "no_soffice"}, ConverterUnavailable),
        (429, {"error": "busy"}, ConverterUnavailable),
        (422, {"error": "conversion_failed"}, ExtractionFailed),
        (504, {"error": "timeout"}, ExtractionFailed),
    ],
)
async def test_converter_errors(
    tmp_path: Path, status: int, body: dict[str, str], error: type[Exception]
) -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(status, json=body))
    out = tmp_path / "out.pdf"
    with pytest.raises(error):
        await _client(transport).convert(f.write(tmp_path / "in.docx", b"PK"), "docx", "pdf", out)
    assert not out.exists()


async def test_converter_network_error_is_retryable(tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ConverterUnavailable):
        await _client(httpx.MockTransport(handle)).convert(
            f.write(tmp_path / "in.doc", b"x"), "doc", "docx", tmp_path / "o"
        )


async def test_converter_rejects_empty_result(tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, content=b""))
    with pytest.raises(ExtractionFailed):
        await _client(transport).convert(f.write(tmp_path / "in.doc", b"x"), "doc", "docx", tmp_path / "o")
    assert not (tmp_path / "o").exists()
