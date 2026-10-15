"""Проверка, что схема БД соответствует последней миграции (без её применения)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncConnection

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


@lru_cache(maxsize=1)
def expected_heads() -> frozenset[str]:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return frozenset(ScriptDirectory.from_config(cfg).get_heads())


async def current_revisions(conn: AsyncConnection) -> frozenset[str]:
    try:
        result = await conn.execute(text("SELECT version_num FROM alembic_version"))
    except ProgrammingError:
        return frozenset()
    return frozenset(row[0] for row in result)
