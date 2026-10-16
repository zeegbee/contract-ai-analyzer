"""Разбор недоверенных файлов в дочернем процессе (ARCHITECTURE §8.2).

Родитель (worker) запускает `python -m app.ingestion.sandbox <mode> <path> ...`:
- окружение минимальное — без DATABASE_URL, SECRET_KEY и прочих секретов;
- дочерний процесс сам ставит лимиты RLIMIT_AS (память), RLIMIT_CPU, RLIMIT_NOFILE, RLIMIT_CORE
  (только Linux/macOS; на Windows — только таймаут);
- родитель ждёт с таймаутом и убивает всю группу процессов;
- результат — одна строка `RESULT:<json>` в stdout.

Падение парсера, исчерпание памяти или зависание не роняют worker: родитель получает код ошибки.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.ingestion.model import ExtractionFailed, NoTextLayer

RESULT_PREFIX = "RESULT:"
MODES = ("extract-pdf", "extract-docx", "pdf-page-texts")
_POSIX = os.name == "posix"
_BACKEND_ROOT = Path(__file__).resolve().parents[2]


class SandboxError(ExtractionFailed):
    """Сбой дочернего процесса (таймаут, лимит ресурсов, падение)."""


@dataclass(frozen=True)
class SandboxLimits:
    timeout_seconds: int
    memory_mb: int
    max_pages: int


# ---------------------------------------------------------------- дочерний процесс


def _apply_limits(memory_mb: int, cpu_seconds: int) -> None:
    if sys.platform == "win32":
        return
    import resource  # noqa: PLC0415 - только POSIX

    memory = memory_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 5))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))


def _run_mode(mode: str, path: Path, max_pages: int) -> dict[str, Any]:
    # Импорт после установки лимитов: разбор и загрузка библиотек — уже под ограничениями.
    if mode == "extract-pdf":
        from app.ingestion.pdf import PdfLimits, extract_pdf  # noqa: PLC0415

        return extract_pdf(path, PdfLimits(max_pages=max_pages)).to_json()
    if mode == "extract-docx":
        from app.ingestion.docx import DocxLimits, extract_docx  # noqa: PLC0415

        return extract_docx(path, DocxLimits(max_pages=max_pages)).to_json()
    from app.ingestion.pdf import pdf_page_texts  # noqa: PLC0415

    return {"pages": pdf_page_texts(path, max_pages)}


def _emit(payload: dict[str, Any]) -> None:
    # Явно UTF-8: кодировка stdout зависит от платформы, а `-E` отключает PYTHONIOENCODING.
    line = RESULT_PREFIX + json.dumps(payload, ensure_ascii=False) + "\n"
    sys.stdout.flush()
    sys.stdout.buffer.write(line.encode("utf-8"))
    sys.stdout.buffer.flush()


def child_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.ingestion.sandbox")
    parser.add_argument("mode", choices=MODES)
    parser.add_argument("path")
    parser.add_argument("--max-pages", type=int, required=True)
    parser.add_argument("--memory-mb", type=int, required=True)
    parser.add_argument("--cpu-seconds", type=int, required=True)
    args = parser.parse_args(argv)
    _apply_limits(args.memory_mb, args.cpu_seconds)
    try:
        result = _run_mode(args.mode, Path(args.path), args.max_pages)
    except NoTextLayer as exc:
        _emit({"ok": False, "code": exc.code, "no_text": True})
        return 0
    except ExtractionFailed as exc:
        _emit({"ok": False, "code": exc.code})
        return 0
    except MemoryError:
        _emit({"ok": False, "code": "resource_limit_exceeded"})
        return 0
    except RecursionError:
        _emit({"ok": False, "code": "corrupted_file"})
        return 0
    _emit({"ok": True, "result": result})
    return 0


# ---------------------------------------------------------------- родитель


def _child_env() -> dict[str, str]:
    # PYTHON* не передаются: дочерний процесс запускается с `-E`.
    env = {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"}
    # Без SYSTEMROOT Python на Windows не стартует (только для разработки и тестов).
    for key in ("SYSTEMROOT", "TEMP", "TMP"):
        if key in os.environ and not _POSIX:
            env[key] = os.environ[key]
    return env


def _kill(proc: subprocess.Popen[bytes]) -> None:
    try:
        if sys.platform != "win32":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def run_sandbox(mode: str, path: Path, limits: SandboxLimits) -> dict[str, Any]:
    """Блокирующий запуск дочернего процесса (вызывать через asyncio.to_thread)."""
    if mode not in MODES:
        raise ValueError(mode)
    cmd = [
        sys.executable,
        "-E",  # игнорировать PYTHON* из окружения
        "-s",  # без user site-packages
        "-B",  # не писать .pyc (FS контейнера только для чтения)
        "-m",
        "app.ingestion.sandbox",
        mode,
        str(path),
        "--max-pages",
        str(limits.max_pages),
        "--memory-mb",
        str(limits.memory_mb),
        "--cpu-seconds",
        str(limits.timeout_seconds),
    ]
    popen_kwargs: dict[str, Any] = {"start_new_session": True} if _POSIX else {}
    with subprocess.Popen(  # noqa: S603 - аргументы формирует сервер, не пользователь
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_child_env(),
        cwd=str(_BACKEND_ROOT),
        **popen_kwargs,
    ) as proc:
        try:
            stdout, _stderr = proc.communicate(timeout=limits.timeout_seconds)
        except subprocess.TimeoutExpired:
            _kill(proc)
            proc.communicate()
            raise SandboxError("extraction_timeout") from None
    if proc.returncode != 0:
        # stderr не логируется: в трассировке парсера может оказаться фрагмент документа.
        killed = proc.returncode < 0 or proc.returncode in (137, 152)
        raise SandboxError("resource_limit_exceeded" if killed else "extractor_crashed")
    for line in reversed(stdout.decode("utf-8", errors="replace").splitlines()):
        if line.startswith(RESULT_PREFIX):
            payload: dict[str, Any] = json.loads(line[len(RESULT_PREFIX) :])
            break
    else:
        raise SandboxError("extractor_crashed")
    if not payload.get("ok"):
        code = str(payload.get("code") or "extractor_crashed")
        raise NoTextLayer(code) if payload.get("no_text") else ExtractionFailed(code)
    result: dict[str, Any] = payload["result"]
    return result


if __name__ == "__main__":
    sys.exit(child_main())
