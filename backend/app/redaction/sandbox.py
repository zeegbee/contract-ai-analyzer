"""Обнаружение в дочернем процессе с лимитами (как разбор файлов, `app.ingestion.sandbox`).

NER и морфология занимают память и CPU; длинный документ не должен останавливать worker. Входные
данные (фрагменты и настройки) передаются через stdin, результат — строка `RESULT:<json>` в stdout.
Окружение дочернего процесса — без секретов.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.ingestion.sandbox import (
    _BACKEND_ROOT,
    _POSIX,
    RESULT_PREFIX,
    _apply_limits,
    _child_env,
    _emit,
    _kill,
)


class ScanFailed(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ScanLimits:
    timeout_seconds: int
    memory_mb: int


def child_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.redaction.sandbox")
    parser.add_argument("--memory-mb", type=int, required=True)
    parser.add_argument("--cpu-seconds", type=int, required=True)
    args = parser.parse_args(argv)
    _apply_limits(args.memory_mb, args.cpu_seconds)
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    # Импорт после установки лимитов: загрузка моделей — уже под ограничениями.
    from app.redaction.scan import ScanConfig, scan  # noqa: PLC0415

    try:
        result = scan([(k, t) for k, t in payload["fragments"]], ScanConfig.from_json(payload["config"]))
    except MemoryError:
        _emit({"ok": False, "code": "resource_limit_exceeded"})
        return 0
    _emit({"ok": True, "result": result})
    return 0


def run_scan(
    fragments: Sequence[tuple[str, str]], config: dict[str, Any], limits: ScanLimits
) -> dict[str, Any]:
    """Блокирующий запуск (вызывать через asyncio.to_thread)."""
    cmd = [
        sys.executable,
        "-E",
        "-s",
        "-B",
        "-m",
        "app.redaction.sandbox",
        "--memory-mb",
        str(limits.memory_mb),
        "--cpu-seconds",
        str(limits.timeout_seconds),
    ]
    data = json.dumps({"fragments": list(fragments), "config": config}, ensure_ascii=False).encode("utf-8")
    popen_kwargs: dict[str, Any] = {"start_new_session": True} if _POSIX else {}
    with subprocess.Popen(  # noqa: S603 - аргументы формирует сервер
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # Один поток BLAS: иначе резерв адресного пространства потоков упирается в RLIMIT_AS.
        env={**_child_env(), "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"},
        cwd=str(_BACKEND_ROOT),
        **popen_kwargs,
    ) as proc:
        try:
            stdout, _stderr = proc.communicate(data, timeout=limits.timeout_seconds)
        except subprocess.TimeoutExpired:
            _kill(proc)
            proc.communicate()
            raise ScanFailed("redaction_scan_timeout") from None
    if proc.returncode != 0:
        # stderr не логируется: в трассировке может оказаться фрагмент текста.
        killed = proc.returncode < 0 or proc.returncode in (137, 152)
        raise ScanFailed("resource_limit_exceeded" if killed else "redaction_scan_crashed")
    for line in reversed(stdout.decode("utf-8", errors="replace").splitlines()):
        if line.startswith(RESULT_PREFIX):
            payload: dict[str, Any] = json.loads(line[len(RESULT_PREFIX) :])
            break
    else:
        raise ScanFailed("redaction_scan_crashed")
    if not payload.get("ok"):
        raise ScanFailed(str(payload.get("code") or "redaction_scan_crashed"))
    result: dict[str, Any] = payload["result"]
    return result


if __name__ == "__main__":
    sys.exit(child_main())
