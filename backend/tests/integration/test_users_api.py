"""Управление пользователями: роли, блокировка, сброс пароля, сессии, модели, аудит."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from app.models.enums import UserRole
from tests.integration.conftest import DEFAULT_PASSWORD, ApiEnv, creds

pytestmark = pytest.mark.integration

NEW_PASSWORD = "Copper-Meadow-Falcon-52"


async def _audit_actions(api: ApiEnv, target_id: uuid.UUID) -> list[str]:
    rows = await api.rows("SELECT action FROM audit_log WHERE target_id = :t ORDER BY id", t=str(target_id))
    return [r[0] for r in rows]


@pytest.mark.parametrize("role", [UserRole.analyst, UserRole.viewer])
async def test_non_admins_cannot_use_user_management(api: ApiEnv, role: UserRole) -> None:
    actor = await api.create_user(role)
    victim = await api.create_user(UserRole.viewer)
    c = await api.login(actor)
    async with c:
        requests = [
            c.get("/api/users"),
            c.get(f"/api/users/{victim.id}"),
            c.post("/api/users", json={"email": "x@example.org", "display_name": "X", "role": "viewer"}),
            c.patch(f"/api/users/{victim.id}", json={"role": "admin"}),
            c.patch(f"/api/users/{actor.id}", json={"role": "superadmin"}),
            c.post(f"/api/users/{victim.id}/reset-password"),
            c.post(f"/api/users/{victim.id}/revoke-sessions"),
            c.post(f"/api/users/{victim.id}/unlock"),
            c.get(f"/api/users/{victim.id}/model-permissions"),
            c.put(f"/api/users/{victim.id}/model-permissions", json={"overrides": []}),
            c.get("/api/admin/audit"),
        ]
        for request in requests:
            r = await request
            assert r.status_code == 403, r.request.url
            assert r.json()["error"]["code"] == "permission_denied"
    assert await api.scalar("SELECT role FROM users WHERE id = :u", u=actor.id) == role.value
    assert await api.scalar("SELECT count(*) FROM users") == 2


async def test_admin_creates_user_with_one_time_password(api: ApiEnv) -> None:
    admin = await api.create_user(UserRole.admin)
    c = await api.login(admin)
    async with c:
        r = await c.post(
            "/api/users",
            json={"email": " New.User@Example.org ", "display_name": "  Новый  ", "role": "analyst"},
        )
        assert r.status_code == 201
        body = r.json()
        temporary = body["temporary_password"]
        assert body["user"]["email"] == "new.user@example.org"
        assert body["user"]["must_change_password"] is True
        new_id = body["user"]["id"]
        assert temporary not in json.dumps((await c.get(f"/api/users/{new_id}")).json())

        dup = await c.post(
            "/api/users", json={"email": "NEW.USER@example.org", "display_name": "Д", "role": "viewer"}
        )
        assert dup.status_code == 409 and dup.json()["error"]["code"] == "email_taken"

    stored = await api.scalar("SELECT password_hash FROM users WHERE id = :u", u=new_id)
    assert temporary not in stored
    async with api.client() as nc:
        r = await nc.post("/api/auth/login", json=creds("new.user@example.org", temporary))
        assert r.status_code == 200 and r.json()["must_change_password"] is True
    audit = await api.rows("SELECT details FROM audit_log WHERE action = 'user.created'")
    assert audit[0][0] == {"role": "analyst"}
    assert all(temporary not in r[0] for r in await api.rows("SELECT details::text FROM audit_log"))


async def test_admin_cannot_create_or_promote_superadmin(api: ApiEnv) -> None:
    admin = await api.create_user(UserRole.admin)
    analyst = await api.create_user(UserRole.analyst)
    c = await api.login(admin)
    async with c:
        r = await c.post(
            "/api/users", json={"email": "s@example.org", "display_name": "S", "role": "superadmin"}
        )
        assert r.status_code == 403 and r.json()["error"]["code"] == "role_not_assignable"
        r = await c.patch(f"/api/users/{analyst.id}", json={"role": "superadmin"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "role_not_assignable"
        r = await c.patch(f"/api/users/{admin.id}", json={"role": "superadmin"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "cannot_change_own_role"
    assert await api.scalar("SELECT count(*) FROM users WHERE role = 'superadmin'") == 0


async def test_admin_cannot_modify_superadmin(api: ApiEnv) -> None:
    admin = await api.create_user(UserRole.admin)
    root = await api.create_user(UserRole.superadmin)
    c = await api.login(admin)
    async with c:
        assert (await c.get(f"/api/users/{root.id}")).status_code == 200  # чтение разрешено
        for request in (
            c.patch(f"/api/users/{root.id}", json={"role": "viewer"}),
            c.patch(f"/api/users/{root.id}", json={"is_active": False}),
            c.patch(f"/api/users/{root.id}", json={"display_name": "Взлом"}),
            c.post(f"/api/users/{root.id}/reset-password"),
            c.post(f"/api/users/{root.id}/revoke-sessions"),
            c.put(f"/api/users/{root.id}/model-permissions", json={"overrides": []}),
        ):
            r = await request
            assert r.status_code == 403
            assert r.json()["error"]["code"] == "superadmin_protected"
    row = (await api.rows("SELECT role, is_active, display_name FROM users WHERE id = :u", u=root.id))[0]
    assert tuple(row) == ("superadmin", True, "Тест superadmin")


async def test_role_change_by_admin_and_audit(api: ApiEnv) -> None:
    admin = await api.create_user(UserRole.admin)
    target = await api.create_user(UserRole.viewer)
    c = await api.login(admin)
    async with c:
        r = await c.patch(f"/api/users/{target.id}", json={"role": "analyst", "display_name": "Аналитик"})
        assert r.status_code == 200
        assert r.json()["role"] == "analyst" and r.json()["display_name"] == "Аналитик"
    assert await _audit_actions(api, target.id) == ["user.updated", "user.role_changed"]
    details = await api.scalar("SELECT details FROM audit_log WHERE action = 'user.role_changed'")
    assert details == {"from": "viewer", "to": "analyst"}
    actor = await api.scalar("SELECT actor_user_id FROM audit_log WHERE action = 'user.role_changed'")
    assert actor == admin.id


async def test_role_change_takes_effect_immediately(api: ApiEnv) -> None:
    root = await api.create_user(UserRole.superadmin)
    admin = await api.create_user(UserRole.admin)
    rc = await api.login(root)
    ac = await api.login(admin)
    async with rc, ac:
        assert (await ac.get("/api/users")).status_code == 200
        assert (await rc.patch(f"/api/users/{admin.id}", json={"role": "viewer"})).status_code == 200
        assert (await ac.get("/api/users")).status_code == 403


async def test_superadmins_manage_each_other_but_not_themselves(api: ApiEnv) -> None:
    root = await api.create_user(UserRole.superadmin)
    second = await api.create_user(UserRole.superadmin)
    c = await api.login(root)
    async with c:
        r = await c.patch(f"/api/users/{root.id}", json={"is_active": False})
        assert r.json()["error"]["code"] == "cannot_block_self"
        assert (await c.patch(f"/api/users/{second.id}", json={"role": "admin"})).status_code == 200
        assert (await c.patch(f"/api/users/{second.id}", json={"role": "superadmin"})).status_code == 200
        assert (await c.patch(f"/api/users/{second.id}", json={"is_active": False})).status_code == 200


async def test_concurrent_mutual_demotion_keeps_a_superadmin(api: ApiEnv) -> None:
    a = await api.create_user(UserRole.superadmin)
    b = await api.create_user(UserRole.superadmin)
    ca = await api.login(a)
    cb = await api.login(b)
    async with ca, cb:
        ra, rb = await asyncio.gather(
            ca.patch(f"/api/users/{b.id}", json={"role": "viewer"}),
            cb.patch(f"/api/users/{a.id}", json={"role": "viewer"}),
        )
    # Изменения сериализуются: второе выполняется от имени уже пониженного пользователя и отклоняется.
    assert sorted([ra.status_code, rb.status_code]) == [200, 403]
    assert await api.scalar("SELECT count(*) FROM users WHERE role = 'superadmin' AND is_active") == 1


async def test_last_active_superadmin_invariant(api: ApiEnv) -> None:
    """Защита в глубину: инвариант проверяется под блокировкой и без участия политики."""
    from app.services.users import LastSuperadmin

    root = await api.create_user(UserRole.superadmin)
    await api.create_user(UserRole.superadmin, is_active=False)
    service = api.app.state.user_service
    async with api.factory() as db:
        target = await db.get_one(type(root), root.id)
        with pytest.raises(LastSuperadmin):
            await service._ensure_not_last_superadmin(db, target)
    await api.create_user(UserRole.superadmin)
    async with api.factory() as db:
        await service._ensure_not_last_superadmin(db, await db.get_one(type(root), root.id))


async def test_blocked_or_demoted_admin_loses_rights_immediately(api: ApiEnv) -> None:
    admin = await api.create_user(UserRole.admin)
    victim = await api.create_user(UserRole.viewer)
    c = await api.login(admin)
    async with c:
        await api.execute("UPDATE users SET role = 'analyst' WHERE id = :u", u=admin.id)
        r = await c.patch(f"/api/users/{victim.id}", json={"role": "admin"})
        assert r.status_code == 403
    assert await api.scalar("SELECT role FROM users WHERE id = :u", u=victim.id) == "viewer"


async def test_blocking_revokes_sessions_and_unblock(api: ApiEnv) -> None:
    admin = await api.create_user(UserRole.admin)
    target = await api.create_user(UserRole.analyst)
    ac = await api.login(admin)
    tc = await api.login(target)
    async with ac, tc:
        assert (await tc.get("/api/auth/me")).status_code == 200
        r = await ac.patch(f"/api/users/{target.id}", json={"is_active": False})
        assert r.status_code == 200 and r.json()["is_active"] is False and r.json()["active_sessions"] == 0
        assert (await tc.get("/api/auth/me")).status_code == 401
        assert (await ac.patch(f"/api/users/{target.id}", json={"is_active": True})).status_code == 200
    await api.login(target)
    assert await _audit_actions(api, target.id) == [
        "auth.login_success",
        "user.blocked",
        "user.unblocked",
        "auth.login_success",
    ]


async def test_reset_password_flow(api: ApiEnv) -> None:
    admin = await api.create_user(UserRole.admin)
    target = await api.create_user(UserRole.analyst)
    ac = await api.login(admin)
    tc = await api.login(target)
    async with ac, tc:
        r = await ac.post(f"/api/users/{target.id}/reset-password")
        assert r.status_code == 200
        temporary = r.json()["temporary_password"]
        assert r.json()["user"]["must_change_password"] is True
        assert (await tc.get("/api/auth/me")).status_code == 401  # сессии отозваны
        own = await ac.post(f"/api/users/{admin.id}/reset-password")
        assert own.status_code == 403 and own.json()["error"]["code"] == "cannot_reset_own_password"
    async with api.client() as c:
        old = await c.post("/api/auth/login", json=creds(target.email, DEFAULT_PASSWORD))
        assert old.status_code == 401
    nc = await api.login(target, temporary)
    async with nc:
        assert (await nc.get("/api/documents")).json()["error"]["code"] == "password_change_required"
        r = await nc.post(
            "/api/auth/change-password", json={"current_password": temporary, "new_password": NEW_PASSWORD}
        )
        assert r.status_code == 200
    details = await api.scalar("SELECT details::text FROM audit_log WHERE action = 'user.password_reset'")
    assert temporary not in details


async def test_admin_revokes_sessions(api: ApiEnv) -> None:
    admin = await api.create_user(UserRole.admin)
    target = await api.create_user(UserRole.viewer)
    ac = await api.login(admin)
    t1 = await api.login(target)
    t2 = await api.login(target)
    async with ac, t1, t2:
        assert (await ac.get(f"/api/users/{target.id}")).json()["active_sessions"] == 2
        r = await ac.post(f"/api/users/{target.id}/revoke-sessions")
        assert r.json() == {"revoked": 2}
        assert (await t1.get("/api/auth/me")).status_code == 401
        assert (await t2.get("/api/auth/me")).status_code == 401
        assert (await ac.get("/api/auth/me")).status_code == 200


async def test_admin_unlocks_login(api: ApiEnv) -> None:
    admin = await api.create_user(UserRole.admin)
    target = await api.create_user(UserRole.analyst)
    async with api.client() as c:
        for _ in range(api.settings.login_max_failures_per_email):
            await c.post("/api/auth/login", json=creds(target.email, "Wrong-Password-123"))
        assert (
            await c.post("/api/auth/login", json=creds(target.email, DEFAULT_PASSWORD))
        ).status_code == 429
        ac = await api.login(admin)
        async with ac:
            r = await ac.post(f"/api/users/{target.id}/unlock")
            assert r.status_code == 200 and r.json()["locked_until"] is None
        assert (
            await c.post("/api/auth/login", json=creds(target.email, DEFAULT_PASSWORD))
        ).status_code == 200


async def test_users_list_filters_and_pagination(api: ApiEnv) -> None:
    admin = await api.create_user(UserRole.admin, email="boss@example.org")
    for i in range(5):
        await api.create_user(UserRole.viewer, email=f"viewer{i}@example.org")
    await api.create_user(UserRole.analyst, email="an_alyst%@example.org", is_active=False)
    c = await api.login(admin)
    async with c:
        seen: list[str] = []
        cursor = None
        while True:
            params = {"limit": "3", **({"cursor": cursor} if cursor else {})}
            page = (await c.get("/api/users", params=params)).json()
            seen += [u["email"] for u in page["items"]]
            cursor = page["next_cursor"]
            if not cursor:
                break
        assert len(seen) == len(set(seen)) == 7
        viewers = (await c.get("/api/users", params={"role": "viewer"})).json()["items"]
        assert len(viewers) == 5
        inactive = (await c.get("/api/users", params={"is_active": "false"})).json()["items"]
        assert [u["email"] for u in inactive] == ["an_alyst%@example.org"]
        # % и _ в поиске — литералы, а не шаблоны
        assert len((await c.get("/api/users", params={"q": "%"})).json()["items"]) == 1
        assert len((await c.get("/api/users", params={"q": "VIEWER"})).json()["items"]) == 5
        assert (await c.get("/api/users", params={"cursor": "garbage!"})).json()["error"][
            "code"
        ] == "invalid_cursor"
        r = await c.get(f"/api/users/{uuid.uuid4()}")
        assert r.status_code == 404 and r.json()["error"]["code"] == "user_not_found"


async def test_model_permissions(api: ApiEnv) -> None:
    admin = await api.create_user(UserRole.admin)
    target = await api.create_user(UserRole.analyst)
    provider_id, m_allowed, m_denied, m_disabled = (uuid.uuid4() for _ in range(4))
    await api.execute(
        "INSERT INTO provider_configs"
        " (id, name, type, base_url, is_cloud, enabled, timeout_s, max_concurrency) "
        "VALUES (:id, 'Ollama', 'ollama', 'http://ollama:11434', false, true, 60, 2)",
        id=provider_id,
    )
    for mid, name, enabled, roles in (
        (m_allowed, "A", True, "{analyst,admin}"),
        (m_denied, "B", True, "{admin}"),
        (m_disabled, "C", False, "{analyst}"),
    ):
        await api.execute(
            "INSERT INTO model_configs (id, provider_config_id, display_name, external_model_id, enabled,"
            " purpose, allowed_roles)"
            " VALUES (:id, :p, :n, :n, :e, '{analysis}', CAST(:r AS varchar[]))",
            id=mid,
            p=provider_id,
            n=name,
            e=enabled,
            r=roles,
        )
    c = await api.login(admin)
    async with c:
        items = (await c.get(f"/api/users/{target.id}/model-permissions")).json()["items"]
        eff = {i["display_name"]: (i["allowed_by_role"], i["override"], i["effective"]) for i in items}
        assert eff == {"A": (True, None, True), "B": (False, None, False), "C": (True, None, False)}

        r = await c.put(
            f"/api/users/{target.id}/model-permissions",
            json={
                "overrides": [
                    {"model_config_id": str(m_allowed), "allowed": False},
                    {"model_config_id": str(m_denied), "allowed": True},
                    {"model_config_id": str(m_disabled), "allowed": True},
                ]
            },
        )
        assert r.status_code == 200
        after = {i["display_name"]: (i["override"], i["effective"]) for i in r.json()["items"]}
        # Персональное разрешение не включает выключенную модель
        assert after == {"A": (False, False), "B": (True, True), "C": (True, False)}

        bad = await c.put(
            f"/api/users/{target.id}/model-permissions",
            json={"overrides": [{"model_config_id": str(uuid.uuid4()), "allowed": True}]},
        )
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "model_not_found"
    assert (
        await api.scalar("SELECT count(*) FROM user_model_permissions WHERE user_id = :u", u=target.id) == 3
    )
    details = await api.scalar(
        "SELECT details FROM audit_log WHERE action = 'user.model_permissions_changed'"
    )
    assert details["changes"][str(m_allowed)] == {"from": None, "to": False}


async def test_audit_api(api: ApiEnv) -> None:
    admin = await api.create_user(UserRole.admin)
    target = await api.create_user(UserRole.viewer)
    c = await api.login(admin)
    async with c:
        await c.patch(f"/api/users/{target.id}", json={"role": "analyst"})
        page = (await c.get("/api/admin/audit", params={"limit": "1"})).json()
        assert page["items"][0]["action"] == "user.role_changed"
        assert page["items"][0]["actor_email"] == admin.email
        assert page["next_cursor"] == page["items"][0]["id"]
        older = (await c.get("/api/admin/audit", params={"before_id": page["next_cursor"]})).json()
        assert [i["action"] for i in older["items"]] == ["auth.login_success"]
        group = (await c.get("/api/admin/audit", params={"action": "auth."})).json()["items"]
        assert {i["action"] for i in group} == {"auth.login_success"}
        assert (await c.get("/api/admin/audit", params={"action": "x;drop"})).status_code == 422
        by_target = (await c.get("/api/admin/audit", params={"target_id": str(target.id)})).json()["items"]
        assert [i["action"] for i in by_target] == ["user.role_changed"]
