"""Сквозная проверка защиты по всем маршрутам API (этап 08).

Маршруты берутся из OpenAPI-схемы приложения, а не из списка в тесте: новый эндпоинт
автоматически попадает под проверки 401/CSRF/RBAC/IDOR. Если маршрут нельзя классифицировать
(не публичный, не админский, без параметра объекта и не в `SELF_ROUTES`), тест падает —
нужно осознанно решить, какие проверки к нему относятся.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError

from app.models.enums import UserRole
from app.models.users import User
from tests.integration.conftest import ApiClient, ApiEnv

pytestmark = pytest.mark.integration

PUBLIC_ROUTES = {
    ("GET", "/api/health/live"),
    ("GET", "/api/health/ready"),
    ("POST", "/api/auth/login"),
}
# Маршруты без ID объекта в пути: работают с собственными данными пользователя или списками,
# которые фильтруются по правам (проверяются отдельными тестами).
SELF_ROUTES = {
    ("POST", "/api/auth/logout"),
    ("GET", "/api/auth/me"),
    ("POST", "/api/auth/change-password"),
    ("GET", "/api/auth/sessions"),
    ("POST", "/api/auth/sessions/{session_id}/revoke"),
    ("POST", "/api/auth/sessions/revoke-others"),
    ("GET", "/api/documents"),
    ("POST", "/api/documents"),
    ("GET", "/api/documents/upload-limits"),
    ("POST", "/api/analyses/estimate"),
    ("GET", "/api/analyses"),
    ("POST", "/api/analyses"),
    ("GET", "/api/models/available"),
    ("POST", "/api/models/{model_id}/cloud-consent"),
}
OBJECT_PARAMS = ("document_id", "version_id", "analysis_id", "export_id")
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
TITLE = "Секретный договор поставки"


def is_admin_route(path: str) -> bool:
    return path.startswith(("/api/admin/", "/api/users"))


def api_routes(api: ApiEnv) -> list[tuple[str, str]]:
    schema: dict[str, Any] = api.app.openapi()
    return sorted(
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        for method in operations
        if method in {"get", "post", "put", "patch", "delete"}
    )


@dataclass
class World:
    owner: User
    stranger: User
    viewer: User
    doc: uuid.UUID
    version: uuid.UUID
    analysis: uuid.UUID
    export: uuid.UUID

    def fill(self, path: str) -> str:
        values = {
            "document_id": self.doc,
            "version_id": self.version,
            "analysis_id": self.analysis,
            "export_id": self.export,
            "user_id": self.stranger.id,
        }
        return re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1), uuid.uuid4())), path)


@pytest.fixture
async def world(api: ApiEnv) -> World:
    owner = await api.create_user(UserRole.analyst)
    stranger = await api.create_user(UserRole.analyst)
    viewer = await api.create_user(UserRole.viewer)
    doc, version, analysis, export = (uuid.uuid4() for _ in range(4))
    await api.execute(
        "INSERT INTO documents (id, owner_id, title, status) VALUES (:id, :o, :t, 'active')",
        id=doc,
        o=owner.id,
        t=TITLE,
    )
    await api.execute(
        "INSERT INTO document_versions (id, document_id, version_no, original_filename, detected_format,"
        " mime_type, size_bytes, sha256, storage_key, uploaded_by_id, extraction_status)"
        " VALUES (:id, :d, 1, 'contract.pdf', 'pdf', 'application/pdf', 1024, :sha, :key, :o, 'ready')",
        id=version,
        d=doc,
        sha="0" * 64,
        key=f"originals/{version.hex}",
        o=owner.id,
    )
    await api.execute("UPDATE documents SET current_version_id = :v WHERE id = :d", v=version, d=doc)
    await api.execute(
        "INSERT INTO analyses (id, document_id, version_id, created_by_id, party, categories, depth,"
        " config_snapshot, cloud_transfer, status)"
        " VALUES (:id, :d, :v, :o, 'unknown', '{payment}', 'standard', '{}'::jsonb, false, 'failed')",
        id=analysis,
        d=doc,
        v=version,
        o=owner.id,
    )
    await api.execute(
        "INSERT INTO exports (id, analysis_id, requested_by_id, format) VALUES (:id, :a, :o, 'pdf')",
        id=export,
        a=analysis,
        o=owner.id,
    )
    return World(owner, stranger, viewer, doc, version, analysis, export)


def body_for(method: str, path: str, world: World) -> dict[str, Any] | None:
    """Корректное тело запроса: проверка доступа не должна маскироваться ошибкой валидации (422)."""
    if method not in MUTATING:
        return None
    if path in {"/api/analyses", "/api/analyses/estimate"}:
        return {
            "document_id": str(world.doc),
            "party": "unknown",
            "categories": ["payment"],
            "model_id": str(uuid.uuid4()),
        }
    if path.endswith("/exports"):
        return {"format": "pdf"}
    if path.endswith("/access") and method == "PUT":
        return {"email": world.stranger.email, "permission": "view"}
    return {}


async def call(client: ApiClient, method: str, path: str, body: dict[str, Any] | None) -> Any:
    return await client.request(method, path, json=body)


async def test_every_route_is_classified(api: ApiEnv) -> None:
    unclassified = [
        (method, path)
        for method, path in api_routes(api)
        if (method, path) not in PUBLIC_ROUTES | SELF_ROUTES
        and not is_admin_route(path)
        and not any(f"{{{p}}}" in path for p in OBJECT_PARAMS)
    ]
    assert unclassified == []
    # Список не устарел: каждый маршрут из SELF_ROUTES и PUBLIC_ROUTES существует.
    assert (SELF_ROUTES | PUBLIC_ROUTES) - set(api_routes(api)) == set()


async def test_every_non_public_route_requires_session(api: ApiEnv, world: World) -> None:
    async with api.client() as anon:
        for method, path in api_routes(api):
            if (method, path) in PUBLIC_ROUTES:
                continue
            r = await call(anon, method, world.fill(path), body_for(method, path, world))
            assert r.status_code == 401, (method, path, r.status_code)
            assert r.json()["error"]["code"] == "authentication_required"


async def test_every_mutating_route_requires_csrf_token(api: ApiEnv, world: World) -> None:
    client = await api.login(world.owner)
    async with client:
        del client.headers["X-CSRF-Token"]
        for method, path in api_routes(api):
            if method not in MUTATING or (method, path) in PUBLIC_ROUTES:
                continue
            r = await call(client, method, world.fill(path), body_for(method, path, world))
            assert r.status_code == 403, (method, path, r.status_code)
            assert r.json()["error"]["code"] == "csrf_failed"
        # Верный токен, но чужой origin — тоже отказ.
        client.headers["X-CSRF-Token"] = client.cookies["cai_csrf"]
        client.headers["Origin"] = "https://evil.example"
        r = await client.post(f"/api/analyses/{world.analysis}/cancel")
        assert r.json()["error"]["code"] == "csrf_failed"
    # Ничего не изменилось
    assert await api.scalar("SELECT status FROM documents WHERE id = :d", d=world.doc) == "active"


@pytest.mark.parametrize("role", [UserRole.analyst, UserRole.viewer])
async def test_admin_routes_forbidden_for_non_admins(api: ApiEnv, world: World, role: UserRole) -> None:
    user = world.stranger if role is UserRole.analyst else world.viewer
    client = await api.login(user)
    async with client:
        for method, path in api_routes(api):
            if not is_admin_route(path):
                continue
            r = await call(client, method, world.fill(path), body_for(method, path, world))
            assert r.status_code == 403, (role, method, path, r.status_code)
            assert r.json()["error"]["code"] == "permission_denied"


async def test_stranger_gets_404_on_every_object_route(api: ApiEnv, world: World) -> None:
    """IDOR: посторонний пользователь (Analyst без доступа) не отличает чужой объект от несуществующего."""
    client = await api.login(world.stranger)
    checked = 0
    async with client:
        for method, path in api_routes(api):
            object_route = any(f"{{{p}}}" in path for p in OBJECT_PARAMS)
            body = body_for(method, path, world)
            if not object_route and path not in {"/api/analyses", "/api/analyses/estimate"}:
                continue
            if path in {"/api/analyses", "/api/analyses/estimate"} and method != "POST":
                continue
            r = await call(client, method, world.fill(path), body)
            assert r.status_code == 404, (method, path, r.status_code, r.text)
            assert r.json()["error"]["code"].endswith("_not_found"), (method, path)
            assert TITLE not in r.text
            checked += 1
    assert checked >= 20
    # Посторонний ничего не изменил и не создал.
    assert await api.scalar("SELECT status FROM documents WHERE id = :d", d=world.doc) == "active"
    assert await api.scalar("SELECT count(*) FROM analyses") == 1
    assert await api.scalar("SELECT count(*) FROM exports") == 1
    assert await api.scalar("SELECT count(*) FROM document_access") == 0
    assert await api.scalar("SELECT count(*) FROM jobs") == 0


async def test_owner_reaches_every_object_route(api: ApiEnv, world: World) -> None:
    """Контроль предыдущего теста: те же ID для владельца ведут к объекту (не опечатка в пути)."""
    client = await api.login(world.owner)
    async with client:
        for method, path in api_routes(api):
            if method != "GET" or not any(f"{{{p}}}" in path for p in OBJECT_PARAMS):
                continue
            r = await client.get(world.fill(path))
            code = r.json().get("error", {}).get("code", "") if r.status_code >= 400 else ""
            # Файлов в хранилище нет и экспорт не готов — это допустимые ответы, но не «объект не найден».
            assert code not in {
                "document_not_found",
                "version_not_found",
                "analysis_not_found",
                "export_not_found",
            }, (path, r.status_code, code)


async def test_audit_log_is_append_only(api: ApiEnv, world: World) -> None:
    """THREAT_MODEL §4.10: запись аудита нельзя изменить или удалить даже прямым SQL роли приложения."""
    await api.login(world.owner)
    count = await api.scalar("SELECT count(*) FROM audit_log")
    assert count >= 1
    for sql in (
        "UPDATE audit_log SET action = 'auth.logout'",
        "DELETE FROM audit_log",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            await api.execute(sql)
    assert await api.scalar("SELECT count(*) FROM audit_log") == count


async def test_cors_is_not_enabled(api: ApiEnv, world: World) -> None:
    """Один origin через nginx: CORS-заголовков нет, браузер не даст чужому сайту прочитать ответ."""
    client = await api.login(world.owner)
    async with client:
        evil = {"Origin": "https://evil.example"}
        preflight = await client.options(
            "/api/documents", headers={**evil, "Access-Control-Request-Method": "DELETE"}
        )
        read = await client.get("/api/documents", headers=evil)
    for r in (preflight, read):
        assert not [h for h in r.headers if h.lower().startswith("access-control-")], r.headers
    assert read.headers["cache-control"] == "no-store"
