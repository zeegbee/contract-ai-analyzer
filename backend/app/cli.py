"""Административные команды: `python -m app.cli bootstrap | rotate-secrets | reset-superadmin-password`.

Коды выхода: 0 — успешно (создан или уже существует), 1 — администратор не создан и отсутствует
(или не найден), 2 — ошибка конфигурации.
"""

from __future__ import annotations

import argparse
import sys

from app.aio import run
from app.config import ConfigurationError, Settings, get_settings
from app.db.session import create_engine, create_session_factory
from app.errors import NotFoundError
from app.logging import configure_logging
from app.security.passwords import PasswordService
from app.security.rate_limit import LoginRateLimiter
from app.security.secretbox import SecretBox
from app.security.sessions import SessionManager
from app.services.bootstrap import (
    FAILED_OUTCOMES,
    BootstrapOutcome,
    log_result,
    run_bootstrap,
    superadmin_exists,
)
from app.services.catalog import rotate_provider_secrets
from app.services.users import UserService


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


async def _rotate_secrets(settings: Settings) -> int:
    """Перешифровать ключи провайдеров первым ключом SECRETS_ENCRYPTION_KEYS (после добавления нового)."""
    box = SecretBox(settings.secrets_encryption_keys)
    if not box.configured:
        print("SECRETS_ENCRYPTION_KEYS не задан", file=sys.stderr)
        return 2
    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as db:
            rotated, failed = await rotate_provider_secrets(db, box)
    finally:
        await engine.dispose()
    print(f"Перешифровано ключей: {rotated}; не расшифровались: {failed}")
    return 1 if failed else 0


async def _reset_superadmin_password(settings: Settings, email: str) -> int:
    """Аварийный сброс: временный пароль выводится один раз, при входе его нужно сменить."""
    service = UserService(PasswordService(), SessionManager(settings), LoginRateLimiter(settings))
    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as db:
            user, temporary = await service.emergency_reset_superadmin(db, email)
    except NotFoundError as exc:
        print(exc.message, file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print(
        f"Временный пароль для {user.email} (показывается один раз, при входе потребуется сменить):",
        temporary,
        sep="\n",
    )
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("bootstrap", help="Создать первого Superadmin из BOOTSTRAP_ADMIN_* (идемпотентно)")
    sub.add_parser(
        "rotate-secrets", help="Перешифровать ключи провайдеров новым ключом SECRETS_ENCRYPTION_KEYS"
    )
    reset = sub.add_parser(
        "reset-superadmin-password",
        help="Аварийно сбросить пароль Superadmin (временный пароль, все сессии завершаются, запись в аудит)",
    )
    reset.add_argument("--email", required=True)
    args = parser.parse_args(argv)

    try:
        settings = get_settings()
    except ConfigurationError as exc:
        print(exc, file=sys.stderr)
        sys.exit(2)
    configure_logging(settings.log_level, settings.log_format)

    if args.command == "bootstrap":
        sys.exit(run(_bootstrap(settings)))
    if args.command == "rotate-secrets":
        sys.exit(run(_rotate_secrets(settings)))
    if args.command == "reset-superadmin-password":
        sys.exit(run(_reset_superadmin_password(settings, args.email)))


if __name__ == "__main__":
    main()
