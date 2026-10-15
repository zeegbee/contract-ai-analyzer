"""Административные команды: `python -m app.cli bootstrap`.

Коды выхода: 0 — успешно (создан или уже существует), 1 — администратор не создан и отсутствует,
2 — ошибка конфигурации.
"""

from __future__ import annotations

import argparse
import sys

from app.aio import run
from app.config import ConfigurationError, Settings, get_settings
from app.db.session import create_engine, create_session_factory
from app.logging import configure_logging
from app.security.passwords import PasswordService
from app.services.bootstrap import (
    FAILED_OUTCOMES,
    BootstrapOutcome,
    log_result,
    run_bootstrap,
    superadmin_exists,
)


async def _bootstrap(settings: Settings) -> int:
    engine = create_engine(settings)
    try:
        factory = create_session_factory(engine)
        async with factory() as db:
            result = await run_bootstrap(db, settings, PasswordService())
        log_result(result, settings)
        print(result.message, file=sys.stderr if result.outcome in FAILED_OUTCOMES else sys.stdout)
        if result.outcome in FAILED_OUTCOMES:
            return 1
        if result.outcome is BootstrapOutcome.disabled:
            async with factory() as db:
                if not await superadmin_exists(db, active_only=True):
                    print("Активного Superadmin нет, а bootstrap отключён — вход невозможен", file=sys.stderr)
                    return 1
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("bootstrap", help="Создать первого Superadmin из BOOTSTRAP_ADMIN_* (идемпотентно)")
    args = parser.parse_args(argv)

    try:
        settings = get_settings()
    except ConfigurationError as exc:
        print(exc, file=sys.stderr)
        sys.exit(2)
    configure_logging(settings.log_level, settings.log_format)

    if args.command == "bootstrap":
        sys.exit(run(_bootstrap(settings)))


if __name__ == "__main__":
    main()
