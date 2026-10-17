"""Настройки резервного копирования (UI + .env), расписание, запрос «сейчас», отчёты скрипта на хосте."""

from __future__ import annotations

import argparse
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app import cli
from app.models.enums import BackupTrigger, UserRole
from app.services import backup as svc
from tests.integration.conftest import ApiEnv

pytestmark = pytest.mark.integration

RECIPIENT = "age1" + "q" * 58


async def superadmin(api: ApiEnv) -> Any:
    return await api.login(await api.create_user(UserRole.superadmin))


async def plan(api: ApiEnv, *, now: datetime | None = None) -> svc.Plan:
    async with api.factory() as db:
        return await svc.plan(db, api.settings, scheduler=True, now=now)


async def report_start(api: ApiEnv, trigger: BackupTrigger = BackupTrigger.scheduled) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with api.factory() as db:
        await svc.report_start(db, run_id, trigger, "none")
    return run_id


async def report_finish(api: ApiEnv, run_id: uuid.UUID, *, success: bool = True, **extra: Any) -> None:
    values: dict[str, Any] = {
        "error_code": None if success else "upload_failed",
        "name": "20261017T030000Z.tar.age",
        "size_bytes": 1024,
        "encrypted": True,
        "uploaded": success,
        "verified": success,
        "pruned_local": 0,
        "pruned_remote": 0,
    }
    values.update(extra)
    async with api.factory() as db:
        await svc.report_finish(db, run_id, success=success, **values)


async def test_defaults_come_from_env_and_admin_reads_only(api: ApiEnv) -> None:
    admin = await api.login(await api.create_user(UserRole.admin))
    analyst = await api.login(await api.create_user(UserRole.analyst))
    async with admin, analyst:
        r = await admin.get("/api/admin/backup")
        assert r.status_code == 200
        body = r.json()
        assert body["config"]["target"] == "none"
        assert body["config"]["schedule"] == "0 3 * * *"
        assert body["config"]["schedule_enabled"] is True
        assert body["config"]["keep_local"] == 7
        assert body["config"]["remote_prune"] == "lifecycle"
        assert set(body["sources"].values()) <= {"env", "default"}
        assert body["problems"] == []
        assert body["can_manage"] is False
        assert body["next_run_at"] is not None
        assert body["scheduler_seen_at"] is None
        assert body["server"]["encryption"]["configured"] is False
        assert (await admin.put("/api/admin/backup", json={"keep_local": 3})).status_code == 403
        assert (await admin.post("/api/admin/backup/run")).status_code == 403
        assert (await analyst.get("/api/admin/backup")).status_code == 403


async def test_superadmin_overrides_and_resets_settings(api: ApiEnv) -> None:
    c = await superadmin(api)
    async with c:
        r = await c.put(
            "/api/admin/backup",
            json={"schedule": "30 2 * * 1-5", "timezone": "Europe/Moscow", "keep_local": 3},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["config"]["schedule"] == "30 2 * * 1-5"
        assert body["config"]["keep_local"] == 3
        assert body["sources"]["schedule"] == "ui"
        assert body["sources"]["target"] == "env"
        assert body["env"]["keep_local"] == 7

        r = await c.put("/api/admin/backup", json={"reset": ["keep_local"]})
        assert r.json()["config"]["keep_local"] == 7
        assert r.json()["sources"]["keep_local"] == "env"

        for bad in (
            {"schedule": "0 25 * * *"},
            {"timezone": "Mars/Base"},
            {"keep_local": 0},
            {"target": "ftp"},
        ):
            r = await c.put("/api/admin/backup", json=bad)
            assert r.status_code == 422, bad
        assert (await c.put("/api/admin/backup", json={"bucket": "x"})).status_code == 422  # не из UI

    changes = await api.rows(
        "SELECT details->'changes' FROM audit_log"
        " WHERE action = 'settings.updated' AND target_id = 'backup' ORDER BY id"
    )
    assert changes[0][0]["schedule"] == {"from": "0 3 * * *", "to": "30 2 * * 1-5"}
    assert changes[1][0] == {"keep_local": {"from": 3, "to": 7}}
    assert await api.scalar("SELECT count(*) FROM config_revisions WHERE entity_id = 'backup'") == 2


async def test_ui_cannot_select_target_not_configured_on_server(api: ApiEnv) -> None:
    c = await superadmin(api)
    async with c:
        r = await c.put("/api/admin/backup", json={"target": "s3"})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "backup_settings_invalid"
        assert set(r.json()["error"]["details"]["problems"]) == {"s3_not_configured", "encryption_required"}
        assert (await c.get("/api/admin/backup")).json()["config"]["target"] == "none"

        # Хранилище и ключ заданы в .env сервера — цель можно выбрать в UI.
        api.settings.backup_s3_endpoint = "https://s3.example.org"
        api.settings.backup_s3_bucket = "cai-backups"
        api.settings.backup_encryption_recipient = RECIPIENT
        r = await c.put("/api/admin/backup", json={"target": "s3"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["config"]["target"] == "s3"
        assert body["config"]["remote_prune"] == "lifecycle"
        assert body["server"]["s3"] == {
            "configured": True,
            "endpoint": "https://s3.example.org",
            "bucket": "cai-backups",
            "prefix": None,
        }
        assert body["server"]["encryption"]["recipient_hint"] == "age1qqqq…qqqqqq"
        assert "ACCESS" not in r.text.upper().replace("ACCESS_KEY", "")  # ключей S3 в ответе нет


async def test_env_problems_are_reported_and_block_plan(
    api: ApiEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    api.settings.backup_target = "local"
    api.settings.backup_local_target_dir = "relative/dir"
    api.settings.backup_keep_local = "many"
    p = await plan(api)
    assert not p.due
    codes = {pr.code for pr in p.config.problems}
    assert codes == {"invalid_keep_local", "encryption_required", "local_dir_not_absolute"}

    capsys.readouterr()
    assert await cli._backup_plan(api.settings, scheduler=True) == 2
    out = capsys.readouterr().out.splitlines()
    assert "STATUS=config_error" in out
    assert "DUE=0" in out
    assert {line for line in out if line.startswith("PROBLEM=")} == {f"PROBLEM={c}" for c in codes}


async def test_schedule_due_logic_and_reports(api: ApiEnv, capsys: pytest.CaptureFixture[str]) -> None:
    now = datetime.now(UTC)
    # Бэкапов ещё не было — первый запуск по расписанию «пора» (догоняющий).
    p = await plan(api, now=now)
    assert p.due and p.trigger is BackupTrigger.scheduled
    capsys.readouterr()
    assert await cli._backup_plan(api.settings, scheduler=True) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[:3] == ["STATUS=ok", "DUE=1", "TRIGGER=scheduled"]
    assert "TARGET=none" in out and "KEEP_LOCAL=7" in out and "REMOTE_PRUNE=lifecycle" in out

    run_id = await report_start(api)
    assert not (await plan(api, now=now)).due  # уже запущен после последнего срабатывания
    await report_finish(api, run_id)

    c = await superadmin(api)
    async with c:
        state = (await c.get("/api/admin/backup")).json()
        assert state["scheduler_seen_at"] is not None
        assert state["last_success_at"] is not None
        assert state["overdue"] is False
        assert state["runs"][0]["status"] == "success"
        assert state["runs"][0]["name"] == "20261017T030000Z.tar.age"

        # Расписание выключено — по расписанию не запускается, даже если давно не было бэкапа.
        await c.put("/api/admin/backup", json={"schedule_enabled": False})
    assert not (await plan(api, now=now + timedelta(days=3))).due

    rows = await api.rows(
        "SELECT action, actor_user_id, details->>'name' FROM audit_log WHERE action LIKE 'backup.%'"
    )
    assert [tuple(r) for r in rows] == [("backup.completed", None, "20261017T030000Z.tar.age")]


async def test_run_now_request(api: ApiEnv) -> None:
    run_id = await report_start(api)
    await report_finish(api, run_id)
    await api.execute("UPDATE backup_runs SET started_at = now() - interval '1 minute'")
    assert not (await plan(api)).due

    c = await superadmin(api)
    async with c:
        r = await c.post("/api/admin/backup/run")
        assert r.status_code == 202
        assert r.json()["requested_at"] is not None
    p = await plan(api)
    assert p.due and p.trigger is BackupTrigger.requested

    await report_start(api, BackupTrigger.requested)
    assert not (await plan(api)).due
    async with c:
        assert (await c.get("/api/admin/backup")).json()["requested_at"] is None
    assert await api.scalar("SELECT count(*) FROM audit_log WHERE action = 'backup.requested'") == 1


async def test_failed_and_interrupted_runs(api: ApiEnv) -> None:
    failed = await report_start(api)
    await report_finish(api, failed, success=False)
    stale = await report_start(api)
    await api.execute("UPDATE backup_runs SET started_at = now() - interval '7 hours' WHERE id = :i", i=stale)
    c = await superadmin(api)
    async with c:
        state = (await c.get("/api/admin/backup")).json()
    by_id = {r["id"]: r for r in state["runs"]}
    assert by_id[str(failed)]["status"] == "failed"
    assert by_id[str(failed)]["error_code"] == "upload_failed"
    assert by_id[str(stale)]["status"] == "interrupted"
    assert state["last_success_at"] is None
    assert state["overdue"] is True
    assert await api.scalar("SELECT count(*) FROM audit_log WHERE action = 'backup.failed'") == 1


async def test_report_cli_validates_input(api: ApiEnv) -> None:
    run_id = uuid.uuid4()
    args = argparse.Namespace(phase="start", run_id=run_id, trigger="manual", target="none")
    assert await cli._backup_report(api.settings, args) == 0
    bad = argparse.Namespace(
        phase="finish",
        run_id=run_id,
        status="success",
        error_code=None,
        name="../../etc/passwd",
        size=1,
        encrypted="1",
        uploaded="0",
        verified="0",
        pruned_local=0,
        pruned_remote=0,
    )
    assert await cli._backup_report(api.settings, bad) == 1
    assert await api.scalar("SELECT status FROM backup_runs WHERE id = :i", i=run_id) == "running"
    bad.name = "20261017T030000Z"
    assert await cli._backup_report(api.settings, bad) == 0
    assert await api.scalar("SELECT status FROM backup_runs WHERE id = :i", i=run_id) == "success"
