"""Административные команды: `python -m app.cli bootstrap | rotate-secrets | reset-superadmin-password |
backup-plan | backup-report`.

`backup-plan` и `backup-report` вызывает скрипт резервного копирования на хосте (`deploy/backup.sh`):
вывод `backup-plan` — только строки `KEY=VALUE` из фиксированного набора ключей, скрипт проверяет каждое
значение и не выполняет вывод как код.

Коды выхода: 0 — успешно (создан или уже существует), 1 — администратор не создан и отсутствует
(или не найден), 2 — ошибка конфигурации.
"""

from __future__ import annotations

import argparse
import sys
import uuid

from app.aio import run
from app.config import ConfigurationError, Settings, get_settings
from app.db.session import create_engine, create_session_factory
from app.errors import AppError, NotFoundError
from app.logging import configure_logging
from app.models.enums import BackupTrigger
from app.security.passwords import PasswordService
from app.security.rate_limit import LoginRateLimiter
from app.security.secretbox import SecretBox
from app.security.sessions import SessionManager
from app.services import backup as backup_service
from app.services import redaction as redaction_service
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
    """Перешифровать первым ключом SECRETS_ENCRYPTION_KEYS (после добавления нового) ключи провайдеров,
    найденные данные предпросмотров маскировки и таблицы соответствий анализов."""
    box = SecretBox(settings.secrets_encryption_keys)
    if not box.configured:
        print("SECRETS_ENCRYPTION_KEYS не задан", file=sys.stderr)
        return 2
    engine = create_engine(settings)
    try:
        factory = create_session_factory(engine)
        async with factory() as db:
            rotated, failed = await rotate_provider_secrets(db, box)
        async with factory() as db:
            masked_rotated, masked_failed = await redaction_service.rotate_secrets(db, box)
    finally:
        await engine.dispose()
    print(f"Перешифровано ключей: {rotated}; не расшифровались: {failed}")
    print(f"Данные маскировки: перешифровано {masked_rotated}; не расшифровались: {masked_failed}")
    return 1 if failed or masked_failed else 0


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


async def _backup_plan(settings: Settings, scheduler: bool) -> int:
    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as db:
            result = await backup_service.plan(db, settings, scheduler=scheduler)
    finally:
        await engine.dispose()
    values = result.config.values
    lines = {
        "STATUS": "ok" if result.config.ok else "config_error",
        "DUE": "1" if result.due else "0",
        "TRIGGER": result.trigger.value if result.trigger else "none",
        "TARGET": values["target"],
        "KEEP_LOCAL": values["keep_local"],
        "KEEP_REMOTE_DAILY": values["keep_remote_daily"],
        "KEEP_REMOTE_WEEKLY": values["keep_remote_weekly"],
        "KEEP_REMOTE_MONTHLY": values["keep_remote_monthly"],
        "REMOTE_PRUNE": values["remote_prune"],
    }
    for key, value in lines.items():
        print(f"{key}={'' if value is None else value}")
    for problem in result.config.problems:
        print(f"PROBLEM={problem.code}")
        print(f"Настройки бэкапа: {problem.message}", file=sys.stderr)
    return 0 if result.config.ok else 2


def _flag(value: str | None) -> bool | None:
    return None if value is None else value == "1"


async def _backup_report(settings: Settings, args: argparse.Namespace) -> int:
    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as db:
            if args.phase == "start":
                await backup_service.report_start(db, args.run_id, BackupTrigger(args.trigger), args.target)
            else:
                await backup_service.report_finish(
                    db,
                    args.run_id,
                    success=args.status == "success",
                    error_code=args.error_code,
                    name=args.name,
                    size_bytes=args.size,
                    encrypted=_flag(args.encrypted),
                    uploaded=_flag(args.uploaded),
                    verified=_flag(args.verified),
                    pruned_local=args.pruned_local,
                    pruned_remote=args.pruned_remote,
                )
    except AppError as exc:
        print(exc.message, file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
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
    plan = sub.add_parser(
        "backup-plan", help="Настройки бэкапа и признак «пора запускать» (для deploy/backup.sh)"
    )
    plan.add_argument("--scheduler", action="store_true", help="вызов из планировщика (отметка опроса)")
    report = sub.add_parser("backup-report", help="Отчёт скрипта бэкапа о запуске (для deploy/backup.sh)")
    report.add_argument("phase", choices=["start", "finish"])
    report.add_argument("--run-id", type=uuid.UUID, required=True)
    report.add_argument("--trigger", choices=[t.value for t in BackupTrigger], default="manual")
    report.add_argument("--target", default="none")
    report.add_argument("--status", choices=["success", "failed"], default="failed")
    report.add_argument("--error-code")
    report.add_argument("--name")
    report.add_argument("--size", type=int)
    report.add_argument("--encrypted", choices=["0", "1"])
    report.add_argument("--uploaded", choices=["0", "1"])
    report.add_argument("--verified", choices=["0", "1"])
    report.add_argument("--pruned-local", type=int)
    report.add_argument("--pruned-remote", type=int)
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
    if args.command == "backup-plan":
        sys.exit(run(_backup_plan(settings, args.scheduler)))
    if args.command == "backup-report":
        sys.exit(run(_backup_report(settings, args)))


if __name__ == "__main__":
    main()
