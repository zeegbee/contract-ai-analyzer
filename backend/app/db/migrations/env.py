"""Окружение Alembic.

Безопасность миграций:
- URL берётся только из DATABASE_URL (не хранится в alembic.ini);
- все миграции выполняются в одной транзакции (PostgreSQL поддерживает транзакционный DDL):
  при ошибке схема остаётся прежней;
- advisory lock не даёт двум процессам применять миграции одновременно;
- lock_timeout не даёт миграции бесконечно ждать блокировку на рабочей БД;
- downgrade в production запрещён без ALLOW_DOWNGRADE=1.
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool, text

import app.models  # noqa: F401 - регистрирует модели в метаданных
from app.db.base import Base

MIGRATION_LOCK_ID = 7_201_005_001  # произвольная константа для pg_advisory_lock

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    url = config.attributes.get("database_url") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL не задан")
    return str(url)


def _guard_downgrade() -> None:
    argv = getattr(config.cmd_opts, "cmd", None)
    command_name = getattr(argv, "__name__", "") if argv else ""
    if (
        command_name == "downgrade"
        and os.environ.get("ENV", "production") == "production"
        and os.environ.get("ALLOW_DOWNGRADE") != "1"
    ):
        raise RuntimeError("downgrade в production запрещён (установите ALLOW_DOWNGRADE=1 после бэкапа)")


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    _guard_downgrade()
    engine = create_engine(_database_url(), poolclass=pool.NullPool, hide_parameters=True)
    with engine.connect() as connection:
        connection.execute(text("SET lock_timeout = '10s'"))
        connection.execute(text("SELECT pg_advisory_lock(:id)"), {"id": MIGRATION_LOCK_ID})
        connection.commit()
        try:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                transaction_per_migration=False,
            )
            with context.begin_transaction():
                context.run_migrations()
            connection.commit()
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": MIGRATION_LOCK_ID})
            connection.commit()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
