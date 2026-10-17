"""Bootstrap первого Superadmin: идемпотентность, параллельный запуск, отсутствие параметров."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.config import Settings
from app.models.enums import UserRole
from app.security.passwords import TEST_PARAMS, PasswordService
from app.services.bootstrap import BootstrapOutcome, run_bootstrap
from tests.conftest import make_settings
from tests.integration.conftest import ApiEnv, creds

pytestmark = pytest.mark.integration

BOOT_EMAIL = "Root@Example.org"
BOOT_PASSWORD = "Obsidian-Harbor-Lantern-64"
PASSWORDS = PasswordService(TEST_PARAMS)


def boot_settings(api: ApiEnv, tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": api.settings.database_url.get_secret_value(),
        "bootstrap_enabled": True,
        "bootstrap_admin_email": BOOT_EMAIL,
        "bootstrap_admin_password": BOOT_PASSWORD,
        "bootstrap_admin_name": "Главный администратор",
    }
    values.update(overrides)
    return make_settings(tmp_path, **values)


async def _run(api: ApiEnv, settings: Settings) -> BootstrapOutcome:
    async with api.factory() as db:
        return (await run_bootstrap(db, settings, PASSWORDS)).outcome


async def test_empty_db_without_parameters_creates_nothing(api: ApiEnv, tmp_path: Path) -> None:
    settings = boot_settings(
        api, tmp_path, bootstrap_admin_email=None, bootstrap_admin_password=None, bootstrap_admin_name=None
    )
    assert await _run(api, settings) is BootstrapOutcome.missing_parameters
    assert await api.scalar("SELECT count(*) FROM users") == 0
    async with api.client() as c:
        ready = await c.get("/api/health/ready")
        assert ready.status_code == 503
        assert ready.json()["checks"]["accounts"] == "bootstrap_required"
        # Входа по «стандартным» учётным данным нет
        for email, password in (("admin@example.org", "admin"), ("admin", "admin"), ("root", "")):
            r = await c.post("/api/auth/login", json=creds(email, password or "x"))
            assert r.status_code == 401


async def test_creates_superadmin_with_forced_password_change(api: ApiEnv, tmp_path: Path) -> None:
    assert await _run(api, boot_settings(api, tmp_path)) is BootstrapOutcome.created
    rows = await api.rows("SELECT email, role, must_change_password, is_active, password_hash FROM users")
    assert len(rows) == 1
    email, role, must_change, active, password_hash = rows[0]
    assert (email, role, must_change, active) == ("root@example.org", "superadmin", True, True)
    assert password_hash.startswith("$argon2id$") and BOOT_PASSWORD not in password_hash
    assert await api.scalar("SELECT value FROM system_settings WHERE key = 'bootstrap_completed'") is True
    assert await api.scalar("SELECT count(*) FROM audit_log WHERE action = 'user.bootstrap_created'") == 1

    async with api.client() as c:
        r = await c.post("/api/auth/login", json=creds(BOOT_EMAIL, BOOT_PASSWORD))
        assert r.status_code == 200 and r.json()["must_change_password"] is True
        assert (await c.get("/api/users")).json()["error"]["code"] == "password_change_required"
        ready = await c.get("/api/health/ready")
        assert ready.json()["checks"]["accounts"] == "ok"


async def test_rerun_does_not_change_password_or_role(api: ApiEnv, tmp_path: Path) -> None:
    await _run(api, boot_settings(api, tmp_path))
    before = await api.rows("SELECT id, password_hash, role, must_change_password FROM users")
    # Повторный запуск с другим паролем и тем же e-mail — ничего не меняется
    other = boot_settings(api, tmp_path, bootstrap_admin_password="Different-Bootstrap-Pass-11")
    assert await _run(api, other) is BootstrapOutcome.skipped_superadmin_exists
    assert await api.rows("SELECT id, password_hash, role, must_change_password FROM users") == before

    # Даже если Superadmin понижен вручную в БД — bootstrap не создаёт нового и не повышает старого
    await api.execute("UPDATE users SET role = 'admin'")
    assert await _run(api, other) is BootstrapOutcome.skipped_already_completed
    assert await api.scalar("SELECT count(*) FROM users") == 1
    assert await api.scalar("SELECT role FROM users") == "admin"


async def test_existing_user_with_same_email_is_not_promoted(api: ApiEnv, tmp_path: Path) -> None:
    user = await api.create_user(UserRole.analyst, email="root@example.org")
    hash_before = await api.scalar("SELECT password_hash FROM users WHERE id = :u", u=user.id)
    assert await _run(api, boot_settings(api, tmp_path)) is BootstrapOutcome.email_conflict
    assert await api.scalar("SELECT role FROM users WHERE id = :u", u=user.id) == "analyst"
    assert await api.scalar("SELECT password_hash FROM users WHERE id = :u", u=user.id) == hash_before


@pytest.mark.parametrize(
    "overrides",
    [
        {"bootstrap_admin_password": "short"},
        {"bootstrap_admin_password": "Password1234"},
        {"bootstrap_admin_email": "not-an-email"},
    ],
)
async def test_invalid_parameters_rejected(api: ApiEnv, tmp_path: Path, overrides: dict[str, object]) -> None:
    assert await _run(api, boot_settings(api, tmp_path, **overrides)) is BootstrapOutcome.invalid_parameters
    assert await api.scalar("SELECT count(*) FROM users") == 0


async def test_disabled_bootstrap_does_nothing(api: ApiEnv, tmp_path: Path) -> None:
    settings = boot_settings(api, tmp_path, bootstrap_enabled=False)
    assert await _run(api, settings) is BootstrapOutcome.disabled
    assert await api.scalar("SELECT count(*) FROM users") == 0


async def test_parallel_bootstrap_creates_single_user(api: ApiEnv, tmp_path: Path) -> None:
    settings = boot_settings(api, tmp_path)
    outcomes = await asyncio.gather(*(_run(api, settings) for _ in range(5)))
    assert outcomes.count(BootstrapOutcome.created) == 1
    assert set(outcomes) == {BootstrapOutcome.created, BootstrapOutcome.skipped_superadmin_exists}
    assert await api.scalar("SELECT count(*) FROM users") == 1


async def test_bootstrap_at_app_startup_and_password_not_logged(
    api: ApiEnv, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.main import create_app

    settings = boot_settings(api, tmp_path, login_delay_step_ms=0)
    app = create_app(settings)  # настраивает логирование на перехваченный stdout
    async with app.router.lifespan_context(app):
        pass
    await app.state.engine.dispose()
    assert await api.scalar("SELECT role FROM users") == "superadmin"
    # Повторный старт: пропуск + напоминание убрать пароль из окружения
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        pass
    await app.state.engine.dispose()
    out = capsys.readouterr().out
    assert '"outcome": "created"' in out
    assert '"outcome": "skipped_superadmin_exists"' in out
    assert "bootstrap_password_still_configured" in out
    assert BOOT_PASSWORD not in out


async def test_cli_exit_codes(api: ApiEnv, tmp_path: Path) -> None:
    from app import cli

    missing = boot_settings(
        api, tmp_path, bootstrap_admin_email=None, bootstrap_admin_password=None, bootstrap_admin_name=None
    )
    assert await cli._bootstrap(missing) == 1
    assert await cli._bootstrap(boot_settings(api, tmp_path)) == 0
    assert await cli._bootstrap(boot_settings(api, tmp_path)) == 0  # повтор — успех без изменений
    assert await cli._bootstrap(boot_settings(api, tmp_path, bootstrap_enabled=False)) == 0
    await api.execute("UPDATE users SET is_active = false")
    assert await cli._bootstrap(boot_settings(api, tmp_path, bootstrap_enabled=False)) == 1


async def test_cli_emergency_superadmin_password_reset(
    api: ApiEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    """Утерян пароль единственного Superadmin: сброс из консоли сервера, вход только с обязательной сменой."""
    from app import cli

    root = await api.create_user(UserRole.superadmin, email="Root@Example.org")
    analyst = await api.create_user(UserRole.analyst)
    old = await api.login(root)
    capsys.readouterr()

    assert await cli._reset_superadmin_password(api.settings, analyst.email) == 1  # только Superadmin
    assert await cli._reset_superadmin_password(api.settings, "nobody@example.org") == 1
    assert await cli._reset_superadmin_password(api.settings, " root@example.ORG ") == 0
    temporary = capsys.readouterr().out.strip().splitlines()[-1]

    async with old:
        assert (await old.get("/api/auth/me")).status_code == 401  # прежние сессии завершены
    async with api.client() as c:
        r = await c.post("/api/auth/login", json={"email": root.email, "password": temporary})
        assert r.status_code == 200
        assert r.json()["must_change_password"] is True
    rows = await api.rows(
        "SELECT actor_user_id, details->>'via' FROM audit_log WHERE action = 'user.password_reset'"
    )
    assert [tuple(r) for r in rows] == [(None, "cli")]
    assert temporary not in str(await api.rows("SELECT details FROM audit_log"))
