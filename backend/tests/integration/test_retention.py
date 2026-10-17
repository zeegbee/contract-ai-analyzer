"""Политика хранения документов: настройка (только Superadmin, подтверждение) и удаление worker'ом."""

from __future__ import annotations

import uuid

import pytest

from app.models.enums import UserRole
from app.models.users import User
from app.services.retention import apply_retention
from tests.analysis_helpers import FakeLLM, run_jobs
from tests.integration.conftest import ApiEnv

pytestmark = pytest.mark.integration


async def make_doc(
    api: ApiEnv,
    owner: User,
    *,
    age_days: int,
    version_age_days: int | None = None,
    analysis_age_days: int | None = None,
    analysis_status: str = "completed",
) -> uuid.UUID:
    doc, version = uuid.uuid4(), uuid.uuid4()
    await api.execute(
        "INSERT INTO documents (id, owner_id, title, status, created_at)"
        " VALUES (:id, :o, 'Договор', 'active', now() - make_interval(days => :age))",
        id=doc,
        o=owner.id,
        age=age_days,
    )
    await api.execute(
        "INSERT INTO document_versions (id, document_id, version_no, original_filename, detected_format,"
        " mime_type, size_bytes, sha256, storage_key, uploaded_by_id, extraction_status, created_at)"
        " VALUES (:id, :d, 1, 'c.pdf', 'pdf', 'application/pdf', 10, :sha, :key, :o, 'ready',"
        " now() - make_interval(days => :age))",
        id=version,
        d=doc,
        sha="0" * 64,
        key=f"originals/{version.hex}",
        o=owner.id,
        age=version_age_days if version_age_days is not None else age_days,
    )
    if analysis_age_days is not None:
        await api.execute(
            "INSERT INTO analyses (id, document_id, version_id, created_by_id, party, categories, depth,"
            " config_snapshot, cloud_transfer, status, created_at)"
            " VALUES (:id, :d, :v, :o, 'unknown', '{payment}', 'standard', '{}'::jsonb, false, :st,"
            " now() - make_interval(days => :age))",
            id=uuid.uuid4(),
            d=doc,
            v=version,
            o=owner.id,
            st=analysis_status,
            age=analysis_age_days,
        )
    return doc


async def status(api: ApiEnv, doc: uuid.UUID) -> str | None:
    value: str | None = await api.scalar("SELECT status FROM documents WHERE id = :d", d=doc)
    return value


async def test_retention_is_off_by_default(api: ApiEnv) -> None:
    owner = await api.create_user(UserRole.analyst)
    old = await make_doc(api, owner, age_days=4000)
    admin = await api.login(await api.create_user(UserRole.admin))
    async with admin:
        r = await admin.get("/api/admin/retention")
    assert r.status_code == 200
    body = r.json()
    assert body["retention_days"] is None
    assert body["eligible_now"] is None
    assert body["can_manage"] is False
    assert await apply_retention(api.factory) == 0
    assert await status(api, old) == "active"


async def test_only_superadmin_changes_retention(api: ApiEnv) -> None:
    admin = await api.login(await api.create_user(UserRole.admin))
    analyst = await api.login(await api.create_user(UserRole.analyst))
    async with admin, analyst:
        r = await admin.put("/api/admin/retention", json={"retention_days": 30, "confirm": True})
        assert r.status_code == 403
        assert (await analyst.get("/api/admin/retention")).status_code == 403
    assert not await api.scalar("SELECT count(*) FROM system_settings WHERE key = 'document_retention'")


async def test_enable_requires_confirmation_and_is_audited(api: ApiEnv) -> None:
    owner = await api.create_user(UserRole.analyst)
    await make_doc(api, owner, age_days=100)
    await make_doc(api, owner, age_days=5)
    root = await api.create_user(UserRole.superadmin)
    c = await api.login(root)
    async with c:
        preview = (await c.get("/api/admin/retention", params={"preview_days": 30})).json()
        assert preview["preview_count"] == 1
        assert preview["can_manage"] is True

        r = await c.put("/api/admin/retention", json={"retention_days": 30})
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "retention_confirmation_required"
        assert r.json()["error"]["details"] == {"eligible": 1}
        assert (await c.get("/api/admin/retention")).json()["retention_days"] is None

        for bad in (0, 3651):
            r = await c.put("/api/admin/retention", json={"retention_days": bad, "confirm": True})
            assert r.status_code == 422

        r = await c.put("/api/admin/retention", json={"retention_days": 30, "confirm": True})
        assert r.status_code == 200, r.text
        assert r.json()["retention_days"] == 30
        assert r.json()["eligible_now"] == 1

        # Увеличение срока и выключение не удаляют больше — подтверждение не нужно; сокращение — нужно.
        assert (await c.put("/api/admin/retention", json={"retention_days": 60})).status_code == 200
        r = await c.put("/api/admin/retention", json={"retention_days": 10})
        assert r.json()["error"]["code"] == "retention_confirmation_required"
        assert (await c.put("/api/admin/retention", json={"retention_days": None})).status_code == 200
        assert (await c.get("/api/admin/retention")).json()["retention_days"] is None

    changes = await api.rows(
        "SELECT details->'changes'->'retention_days' FROM audit_log"
        " WHERE action = 'settings.updated' AND target_id = 'retention' ORDER BY id"
    )
    assert [r[0] for r in changes] == [
        {"from": None, "to": 30},
        {"from": 30, "to": 60},
        {"from": 60, "to": None},
    ]
    assert await api.scalar("SELECT count(*) FROM config_revisions WHERE entity_id = 'retention'") == 3


async def test_worker_deletes_only_inactive_documents(api: ApiEnv) -> None:
    owner = await api.create_user(UserRole.analyst)
    old = await make_doc(api, owner, age_days=100, analysis_age_days=90)
    fresh = await make_doc(api, owner, age_days=5)
    recent_version = await make_doc(api, owner, age_days=100, version_age_days=3)
    recent_analysis = await make_doc(api, owner, age_days=100, analysis_age_days=2)
    running = await make_doc(api, owner, age_days=100, analysis_age_days=60, analysis_status="running")
    root = await api.login(await api.create_user(UserRole.superadmin))
    async with root:
        r = await root.put("/api/admin/retention", json={"retention_days": 30, "confirm": True})
        assert r.json()["eligible_now"] == 1

    assert await apply_retention(api.factory) == 1
    assert await status(api, old) == "deleting"
    for kept in (fresh, recent_version, recent_analysis, running):
        assert await status(api, kept) == "active"
    assert await apply_retention(api.factory) == 0  # повторный проход ничего не трогает

    audit = await api.rows(
        "SELECT actor_user_id, details->>'reason', details->>'retention_days' FROM audit_log"
        " WHERE action = 'document.deleted' AND target_id = :d",
        d=str(old),
    )
    assert [tuple(r) for r in audit] == [(None, "retention", "30")]

    # Задача purge_document удаляет документ целиком — тем же путём, что и ручное удаление.
    assert await run_jobs(api, FakeLLM(), job_types=("purge_document",)) == 1
    assert await status(api, old) is None
    assert await api.scalar("SELECT count(*) FROM analyses WHERE document_id = :d", d=old) == 0


async def test_disabled_policy_stops_deletion(api: ApiEnv) -> None:
    owner = await api.create_user(UserRole.analyst)
    old = await make_doc(api, owner, age_days=100)
    root = await api.login(await api.create_user(UserRole.superadmin))
    async with root:
        await root.put("/api/admin/retention", json={"retention_days": 30, "confirm": True})
        await root.put("/api/admin/retention", json={"retention_days": None})
    assert await apply_retention(api.factory) == 0
    assert await status(api, old) == "active"
