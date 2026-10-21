"""Миграции и readiness на реальном PostgreSQL."""

from __future__ import annotations

import uuid

import httpx
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

import app.models  # noqa: F401
from app.config import Settings
from app.db.base import Base
from app.main import create_app
from tests.conftest import _admin_url, alembic_config

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "users",
    "sessions",
    "login_attempts",
    "document_access",
    "user_model_permissions",
    "consent_records",
    "documents",
    "document_versions",
    "document_pages",
    "document_fragments",
    "analyses",
    "analysis_chunks",
    "analysis_risks",
    "risk_evidence",
    "provider_configs",
    "model_configs",
    "jobs",
    "exports",
    "audit_log",
    "system_settings",
    "config_revisions",
    "worker_heartbeats",
    "backup_runs",
    "redaction_previews",
    "analysis_redactions",
    "alembic_version",
}


def test_schema_matches_models(pg_database_url: str) -> None:
    engine = create_engine(pg_database_url)
    with engine.connect() as conn:
        assert set(inspect(conn).get_table_names()) == EXPECTED_TABLES
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    engine.dispose()
    assert diff == [], f"Модели расходятся с миграциями: {diff}"


def test_check_constraints_enforced(pg_database_url: str) -> None:
    engine = create_engine(pg_database_url)
    with engine.connect() as conn, pytest.raises(Exception, match="ck_jobs_job_status"):
        conn.execute(text("INSERT INTO jobs (type, status) VALUES ('x', 'bogus')"))
    engine.dispose()


def test_upgrade_downgrade_roundtrip_on_clean_db() -> None:
    admin = _admin_url()
    if not admin:
        pytest.skip("TEST_DATABASE_URL не задан")
    name = f"cai_rt_{uuid.uuid4().hex[:8]}"
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(admin).set(database=name).render_as_string(hide_password=False)
    try:
        cfg = alembic_config(url)
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
        engine = create_engine(url)
        with engine.connect() as conn:
            assert "documents" in inspect(conn).get_table_names()
        engine.dispose()
    finally:
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin_engine.dispose()


async def test_ready_with_migrated_database(pg_settings: Settings) -> None:
    app = create_app(pg_settings)
    pg_settings.storage_dir.mkdir(parents=True, exist_ok=True)
    async with app.state.session_factory() as db:
        await db.execute(text("TRUNCATE users CASCADE"))
        await db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        # Без активного Superadmin войти некому — система не готова
        r = await c.get("/api/health/ready")
        assert r.status_code == 503
        assert r.json()["checks"]["accounts"] == "bootstrap_required"
        async with app.state.session_factory() as db:
            await db.execute(
                text(
                    "INSERT INTO users (email, display_name, password_hash, role)"
                    " VALUES ('root@example.org', 'Root', 'x', 'superadmin')"
                )
            )
            await db.commit()
        r = await c.get("/api/health/ready")
        # Worker не запущен: API готов, но это видно в отдельной проверке.
        assert r.status_code == 200, r.text
        assert r.json()["checks"] == {
            "database": "ok",
            "migrations": "ok",
            "accounts": "ok",
            "storage": "ok",
            "worker": "unavailable",
        }
        async with app.state.session_factory() as db:
            await db.execute(
                text("INSERT INTO worker_heartbeats (worker_id, version) VALUES ('w-ready-test', 'test')")
            )
            await db.commit()
        r = await c.get("/api/health/ready")
        async with app.state.session_factory() as db:
            await db.execute(text("TRUNCATE users, worker_heartbeats CASCADE"))
            await db.commit()
    await app.state.engine.dispose()
    assert r.status_code == 200, r.text
    assert r.json()["checks"]["worker"] == "ok"
