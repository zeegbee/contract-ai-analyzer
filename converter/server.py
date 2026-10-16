"""Изолированный сервис конвертации документов (LibreOffice headless), ARCHITECTURE §8.3.

POST /convert?from=doc&to=docx  — DOC → DOCX (дальше backend разбирает DOCX);
POST /convert?from=docx&to=pdf  — DOCX → PDF (только для разметки страниц).
Тело запроса — файл; ответ 200 — результат (application/octet-stream).

Ошибки (JSON {"error": code}): 400 bad_request, 411 length_required, 413 too_large,
415 unsupported, 422 conversion_failed, 429 busy, 504 timeout.

Безопасность:
- контейнер без сети наружу и без доступа к БД/хранилищу, read-only FS, tmpfs, non-root, лимиты CPU/RAM/PIDs;
- на каждый запрос — новый каталог и новый профиль LibreOffice, удаляются после ответа;
- в профиле запрещены макросы (MacroSecurityLevel=3, DisableMacrosExecution) и ссылки с недоверенных
  адресов; Java не установлена;
- одновременно работает один процесс soffice; таймаут убивает всю группу процессов;
- в лог пишутся только метаданные (направление, размер, длительность, результат), без содержимого.

Используется только стандартная библиотека Python — меньше зависимостей в недоверенной зоне.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

SOFFICE = shutil.which("soffice") or shutil.which("libreoffice")
MAX_INPUT_BYTES = int(os.environ.get("CONVERTER_MAX_INPUT_MB", "60")) * 1024 * 1024
TIMEOUT_S = int(os.environ.get("LIBREOFFICE_TIMEOUT_SECONDS", "90"))
QUEUE_WAIT_S = int(os.environ.get("CONVERTER_QUEUE_WAIT_SECONDS", "60"))
CPU_LIMIT_S = TIMEOUT_S + 30
MAX_OUTPUT_FILE_BYTES = 300 * 1024 * 1024

# from → (to → (фильтр LibreOffice, расширение результата))
CONVERSIONS = {
    ("doc", "docx"): ("docx:MS Word 2007 XML", "docx"),
    ("docx", "pdf"): ("pdf:writer_pdf_Export", "pdf"),
}
MAGIC = {"doc": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "docx": b"PK\x03\x04"}

PROFILE_XCU = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
 <item oor:path="/org.openoffice.Office.Common/Security/Scripting">
  <prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>
 <item oor:path="/org.openoffice.Office.Common/Security/Scripting">
  <prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop></item>
 <item oor:path="/org.openoffice.Office.Common/Security/Scripting">
  <prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop></item>
 <item oor:path="/org.openoffice.Office.Common/Misc">
  <prop oor:name="UseOpenCL" oor:op="fuse"><value>false</value></prop></item>
</oor:items>
"""

_LOCK = threading.Lock()


def log(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


class ConversionError(Exception):
    def __init__(self, status: HTTPStatus, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _limits() -> None:
    """Выполняется в дочернем процессе перед запуском soffice (только POSIX)."""
    import resource  # noqa: PLC0415

    resource.setrlimit(resource.RLIMIT_CPU, (CPU_LIMIT_S, CPU_LIMIT_S + 5))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_FILE_BYTES, MAX_OUTPUT_FILE_BYTES))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def convert(data: bytes, source: str, target: str) -> bytes:
    if SOFFICE is None:
        raise ConversionError(HTTPStatus.SERVICE_UNAVAILABLE, "no_soffice")
    spec = CONVERSIONS.get((source, target))
    if spec is None:
        raise ConversionError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "unsupported")
    if not data.startswith(MAGIC[source]):
        raise ConversionError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "unsupported")
    filter_name, out_ext = spec
    workdir = Path(tempfile.mkdtemp(prefix=f"job-{uuid.uuid4().hex}-"))
    try:
        profile = workdir / "profile"
        (profile / "user").mkdir(parents=True, mode=0o700)
        (profile / "user" / "registrymodifications.xcu").write_text(PROFILE_XCU, encoding="utf-8")
        outdir = workdir / "out"
        outdir.mkdir(mode=0o700)
        src = workdir / f"input.{source}"
        src.write_bytes(data)
        cmd = [
            SOFFICE,
            "--headless",
            "--invisible",
            "--norestore",
            "--nolockcheck",
            "--nologo",
            "--nodefault",
            "--nofirststartwizard",
            f"-env:UserInstallation={profile.as_uri()}",
            "--convert-to",
            filter_name,
            "--outdir",
            str(outdir),
            str(src),
        ]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(workdir), "LANG": "C.UTF-8"}
        proc = subprocess.Popen(  # noqa: S603 - аргументы формирует сервис
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=str(workdir),
            start_new_session=True,
            preexec_fn=_limits if sys.platform != "win32" else None,  # noqa: PLW1509
        )
        try:
            proc.wait(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            raise ConversionError(HTTPStatus.GATEWAY_TIMEOUT, "timeout") from None
        _kill_group(proc)  # дочерние процессы soffice, если остались
        result = outdir / f"input.{out_ext}"
        if proc.returncode != 0 or not result.is_file() or result.stat().st_size == 0:
            raise ConversionError(HTTPStatus.UNPROCESSABLE_ENTITY, "conversion_failed")
        return result.read_bytes()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "converter"
    sys_version = ""

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: HTTPStatus, body: dict[str, object]) -> None:
        self._send(status, json.dumps(body).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802 - имя из BaseHTTPRequestHandler
        if self.path == "/health":
            ok = SOFFICE is not None
            self._json(HTTPStatus.OK if ok else HTTPStatus.SERVICE_UNAVAILABLE, {"status": "ok" if ok else "no_soffice"})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        url = urlsplit(self.path)
        if url.path != "/convert":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        query = parse_qs(url.query)
        source = (query.get("from") or [""])[0]
        target = (query.get("to") or [""])[0]
        length_header = self.headers.get("Content-Length")
        if length_header is None or not length_header.isdigit():
            self._json(HTTPStatus.LENGTH_REQUIRED, {"error": "length_required"})
            return
        length = int(length_header)
        if length > MAX_INPUT_BYTES:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "too_large"})
            return
        if (source, target) not in CONVERSIONS:
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "unsupported"})
            return
        data = self.rfile.read(length)
        if len(data) != length:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "bad_request"})
            return
        if not _LOCK.acquire(timeout=QUEUE_WAIT_S):
            self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "busy"})
            return
        started = time.monotonic()
        try:
            result = convert(data, source, target)
        except ConversionError as exc:
            log("conversion_failed", source=source, target=target, size=length, code=exc.code,
                duration_ms=round((time.monotonic() - started) * 1000))  # fmt: skip
            self._json(exc.status, {"error": exc.code})
            return
        finally:
            _LOCK.release()
        log("conversion_done", source=source, target=target, size=length, result_size=len(result),
            duration_ms=round((time.monotonic() - started) * 1000))  # fmt: skip
        self._send(HTTPStatus.OK, result, "application/octet-stream")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Стандартный access-лог отключён: события пишутся через log() без содержимого.
        return


def main() -> None:
    port = int(os.environ.get("PORT", "8090"))
    log("converter_started", soffice=SOFFICE is not None, timeout_s=TIMEOUT_S)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()  # noqa: S104 - внутренняя сеть


if __name__ == "__main__":
    main()
