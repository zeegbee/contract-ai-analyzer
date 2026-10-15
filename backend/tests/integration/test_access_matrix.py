"""IDOR: доступ к чужим документам, анализам и экспортам (роль × свой/чужой/с доступом/несуществующий).

Документы создаются напрямую в БД: загрузка появится на этапе 04, но правила доступа
действуют уже сейчас и проверяются на реальных эндпоинтах и scoped-запросах.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from app.errors import NotFoundError
from app.models.enums import UserRole
from app.models.users import User
from app.security.scoped import DocumentAction, get_analysis_for, get_document_for, get_export_for
from tests.integration.conftest import ApiEnv

pytestmark = pytest.mark.integration


@dataclass
class Fixture:
    users: dict[str, User]
    doc: uuid.UUID  # документ owner, к которому выданы доступы
    analysis: uuid.UUID
    export: uuid.UUID
    other_doc: uuid.UUID  # документ stranger
    deleting_doc: uuid.UUID
    deleting_analysis: uuid.UUID


async def _document(
    api: ApiEnv, owner: User, *, status: str = "active"
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    doc_id, version_id, analysis_id, export_id = (uuid.uuid4() for _ in range(4))
    await api.execute(
        "INSERT INTO documents (id, owner_id, title, status) VALUES (:id, :o, 'Договор поставки', :s)",
        id=doc_id,
        o=owner.id,
        s=status,
    )
    await api.execute(
        "INSERT INTO document_versions (id, document_id, version_no, original_filename, detected_format,"
        " mime_type, size_bytes, sha256, storage_key, uploaded_by_id)"
        " VALUES (:id, :d, 1, 'contract.pdf', 'pdf', 'application/pdf', 1024, :sha, :key, :o)",
        id=version_id,
        d=doc_id,
        sha="0" * 64,
        key=f"originals/{version_id}",
        o=owner.id,
    )
    await api.execute(
        "INSERT INTO analyses (id, document_id, version_id, created_by_id, party, categories, depth,"
        " config_snapshot, cloud_transfer)"
        " VALUES (:id, :d, :v, :o, 'unknown', '{payment}', 'standard', '{}'::jsonb, false)",
        id=analysis_id,
        d=doc_id,
        v=version_id,
        o=owner.id,
    )
    await api.execute(
        "INSERT INTO exports (id, analysis_id, requested_by_id, format) VALUES (:id, :a, :o, 'pdf')",
        id=export_id,
        a=analysis_id,
        o=owner.id,
    )
    return doc_id, analysis_id, export_id


async def _grant(api: ApiEnv, doc_id: uuid.UUID, user: User, permission: str, by: User) -> None:
    await api.execute(
        "INSERT INTO document_access (document_id, user_id, permission, granted_by_id)"
        " VALUES (:d, :u, :p, :g)",
        d=doc_id,
        u=user.id,
        p=permission,
        g=by.id,
    )


@pytest.fixture
async def world(api: ApiEnv) -> Fixture:
    users = {
        "owner": await api.create_user(UserRole.analyst),
        "stranger": await api.create_user(UserRole.analyst),
        "shared_view": await api.create_user(UserRole.viewer),
        "shared_analyze": await api.create_user(UserRole.analyst),
        "viewer_with_analyze": await api.create_user(UserRole.viewer),
        "admin": await api.create_user(UserRole.admin),
        "superadmin": await api.create_user(UserRole.superadmin),
    }
    doc, analysis, export = await _document(api, users["owner"])
    other_doc, _, _ = await _document(api, users["stranger"])
    deleting_doc, deleting_analysis, _ = await _document(api, users["owner"], status="deleting")
    await _grant(api, doc, users["shared_view"], "view", users["owner"])
    await _grant(api, doc, users["shared_analyze"], "analyze", users["owner"])
    await _grant(api, doc, users["viewer_with_analyze"], "analyze", users["owner"])
    return Fixture(users, doc, analysis, export, other_doc, deleting_doc, deleting_analysis)


# Кто может читать документ owner (и его анализы/экспорты) через API.
READ_ACCESS = {
    "owner": True,
    "stranger": False,
    "shared_view": True,
    "shared_analyze": True,
    "viewer_with_analyze": True,
    "admin": False,  # администрирование ≠ доступ к содержимому договоров
    "superadmin": True,  # системный доступ, аудируется
}


@pytest.mark.parametrize(("who", "allowed"), READ_ACCESS.items())
async def test_read_matrix(api: ApiEnv, world: Fixture, who: str, allowed: bool) -> None:
    c = await api.login(world.users[who])
    async with c:
        for path, code in (
            (f"/api/documents/{world.doc}", "document_not_found"),
            (f"/api/analyses/{world.analysis}", "analysis_not_found"),
            (f"/api/exports/{world.export}", "export_not_found"),
        ):
            r = await c.get(path)
            if allowed:
                assert r.status_code == 200, (who, path)
            else:
                # Чужой объект неотличим от несуществующего
                assert r.status_code == 404, (who, path)
                assert r.json()["error"]["code"] == code
                assert "Договор" not in r.text


@pytest.mark.parametrize("who", list(READ_ACCESS))
async def test_nonexistent_and_deleting_objects_are_404(api: ApiEnv, world: Fixture, who: str) -> None:
    c = await api.login(world.users[who])
    async with c:
        for path in (
            f"/api/documents/{uuid.uuid4()}",
            f"/api/analyses/{uuid.uuid4()}",
            f"/api/exports/{uuid.uuid4()}",
            f"/api/documents/{world.deleting_doc}",
            f"/api/analyses/{world.deleting_analysis}",
        ):
            assert (await c.get(path)).status_code == 404, (who, path)
        assert (await c.get("/api/documents/not-a-uuid")).status_code == 422


async def test_stranger_document_hidden_from_everyone_but_owner_and_superadmin(
    api: ApiEnv, world: Fixture
) -> None:
    for who in READ_ACCESS:
        c = await api.login(world.users[who])
        async with c:
            status = (await c.get(f"/api/documents/{world.other_doc}")).status_code
        assert status == (200 if who in {"stranger", "superadmin"} else 404), who


@pytest.mark.parametrize(
    ("who", "analyze", "manage"),
    [
        ("owner", True, True),
        ("stranger", False, False),
        ("shared_view", False, False),
        ("shared_analyze", True, False),
        ("viewer_with_analyze", False, False),  # роль Viewer не позволяет анализ даже при выданном праве
        ("admin", False, False),
        ("superadmin", True, True),
    ],
)
async def test_action_matrix(api: ApiEnv, world: Fixture, who: str, analyze: bool, manage: bool) -> None:
    actor = world.users[who]
    async with api.factory() as db:
        for action, expected in ((DocumentAction.analyze, analyze), (DocumentAction.manage, manage)):
            if expected:
                await get_document_for(db, actor, world.doc, action)
                await get_analysis_for(db, actor, world.analysis, action)
            else:
                with pytest.raises(NotFoundError):
                    await get_document_for(db, actor, world.doc, action)
                with pytest.raises(NotFoundError):
                    await get_analysis_for(db, actor, world.analysis, action)


async def test_role_downgrade_removes_analyze_right(api: ApiEnv, world: Fixture) -> None:
    owner = world.users["owner"]
    await api.execute("UPDATE users SET role = 'viewer' WHERE id = :u", u=owner.id)
    async with api.factory() as db:
        demoted = await db.get_one(User, owner.id)
        await get_document_for(db, demoted, world.doc, DocumentAction.view)
        with pytest.raises(NotFoundError):
            await get_document_for(db, demoted, world.doc, DocumentAction.analyze)
        with pytest.raises(NotFoundError):
            await get_document_for(db, demoted, world.doc, DocumentAction.manage)


async def test_scoped_export_lookup(api: ApiEnv, world: Fixture) -> None:
    async with api.factory() as db:
        assert (await get_export_for(db, world.users["shared_view"], world.export)).id == world.export
        with pytest.raises(NotFoundError):
            await get_export_for(db, world.users["admin"], world.export)


async def test_document_list_is_filtered(api: ApiEnv, world: Fixture) -> None:
    expected = {
        "owner": {world.doc},
        "stranger": {world.other_doc},
        "shared_view": {world.doc},
        "shared_analyze": {world.doc},
        "admin": set(),
        # Superadmin не видит чужие документы в общем списке — только по ID с аудитом
        "superadmin": set(),
    }
    for who, docs in expected.items():
        c = await api.login(world.users[who])
        async with c:
            items = (await c.get("/api/documents")).json()["items"]
        assert {uuid.UUID(i["id"]) for i in items} == docs, who


async def test_superadmin_access_is_audited(api: ApiEnv, world: Fixture) -> None:
    root = world.users["superadmin"]
    c = await api.login(root)
    async with c:
        await c.get(f"/api/documents/{world.doc}")
        await c.get(f"/api/analyses/{world.analysis}")
        await c.get(f"/api/exports/{world.export}")
    rows = await api.rows(
        "SELECT target_id, details->>'via' FROM audit_log"
        " WHERE action = 'document.accessed_by_superadmin' AND actor_user_id = :u ORDER BY id",
        u=root.id,
    )
    assert [tuple(r) for r in rows] == [
        (str(world.doc), "document"),
        (str(world.doc), "analysis"),
        (str(world.doc), "export"),
    ]
    # Название документа в аудит не попадает
    assert not await api.scalar("SELECT count(*) FROM audit_log WHERE details::text LIKE '%Договор%'")

    owner = await api.login(world.users["owner"])
    async with owner:
        await owner.get(f"/api/documents/{world.doc}")
    assert (
        await api.scalar("SELECT count(*) FROM audit_log WHERE action = 'document.accessed_by_superadmin'")
        == 3
    )


async def test_unauthenticated_access_denied(api: ApiEnv, world: Fixture) -> None:
    async with api.client() as c:
        for path in (
            "/api/documents",
            f"/api/documents/{world.doc}",
            f"/api/analyses/{world.analysis}",
            f"/api/exports/{world.export}",
        ):
            assert (await c.get(path)).status_code == 401
