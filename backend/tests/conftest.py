from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.config import Environment, Settings

BACKEND_DIR = Path(__file__).resolve().parent.parent
TEST_SECRET = "test-secret-key-0123456789abcdefghijklmnop"


def pytest_asyncio_loop_factories(config: pytest.Config, item: pytest.Item) -> dict[str, object]:
    # psycopg async не работает с ProactorEventLoop (Windows по умолчанию).
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "env": Environment.test,
        # Порт 1 закрыт: «БД недоступна» для unit-тестов, не требующих PostgreSQL.
        "database_url": "postgresql+psycopg://user:pw@127.0.0.1:1/none",
        "secret_key": TEST_SECRET,
        "secrets_encryption_keys": [Fernet.generate_key().decode()],
        "storage_dir": tmp_path / "storage",
        "allowed_origins": ["http://localhost:8080"],
        "cookie_secure": False,
        "log_format": "json",
        "worker_heartbeat_file": tmp_path / "hb",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


# ---------------------------------------------------------------- integration (PostgreSQL)


def _admin_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL")


def alembic_config(database_url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.attributes["database_url"] = database_url
    return cfg


@pytest.fixture(scope="session")
def pg_database_url() -> Iterator[str]:
    """Создаёт отдельную пустую БД на сессию тестов, применяет миграции, удаляет после."""
    admin = _admin_url()
    if not admin:
        pytest.skip("TEST_DATABASE_URL не задан — интеграционные тесты с PostgreSQL пропущены")
    name = f"cai_test_{uuid.uuid4().hex[:10]}"
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(admin).set(database=name).render_as_string(hide_password=False)
    try:
        command.upgrade(alembic_config(url), "head")
        yield url
    finally:
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin_engine.dispose()


@pytest.fixture
def pg_settings(tmp_path: Path, pg_database_url: str) -> Settings:
    return make_settings(tmp_path, database_url=pg_database_url)


@pytest.fixture
async def session_factory(pg_settings: Settings) -> AsyncIterator[object]:
    from app.db.session import create_engine as create_async
    from app.db.session import create_session_factory

    engine = create_async(pg_settings)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE jobs, worker_heartbeats"))
    yield create_session_factory(engine)
    await engine.dispose()
