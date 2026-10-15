"""Формат ошибок, request ID, заголовки безопасности, health — без PostgreSQL."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from app.config import Environment, Settings
from app.errors import NotFoundError
from app.main import create_app
from tests.conftest import make_settings


class Payload(BaseModel):
    count: int


def _add_test_routes(app: FastAPI) -> None:
    @app.get("/api/test/not-found")
    async def not_found() -> None:
        raise NotFoundError("Документ не найден", code="document_not_found")

    @app.get("/api/test/crash")
    async def crash() -> None:
        raise RuntimeError("internal detail: postgresql+psycopg://u:hidden@db/x")

    @app.post("/api/test/validate")
    async def validate(body: Payload) -> dict[str, int]:
        return {"count": body.count}


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    _add_test_routes(app)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_live(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_ready_reports_unavailable_database(client: httpx.AsyncClient, settings: Settings) -> None:
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    r = await client.get("/api/health/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"] == "unavailable"
    assert body["checks"]["storage"] == "ok"


async def test_app_error_format(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/test/not-found")
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "document_not_found"
    assert err["message"] == "Документ не найден"
    assert err["request_id"] == r.headers["X-Request-ID"]
    assert err["details"] == {}


async def test_unknown_route_uses_error_format(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


async def test_unhandled_error_hides_details(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/test/crash")
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "internal_error"
    assert "hidden" not in r.text
    assert "postgresql" not in r.text


async def test_validation_error_does_not_echo_input(client: httpx.AsyncClient) -> None:
    secret_input = "Конфиденциальный текст договора"
    r = await client.post("/api/test/validate", json={"count": secret_input})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error"
    assert err["details"]["fields"][0]["loc"] == ["body", "count"]
    assert secret_input not in r.text


async def test_security_headers(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/health/live")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Cache-Control"] == "no-store"
    assert "default-src 'none'" in r.headers["Content-Security-Policy"]
    assert "Strict-Transport-Security" not in r.headers  # не production
    assert "server" not in {k.lower() for k in r.headers}


async def test_request_id_propagation(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/health/live", headers={"X-Request-ID": "abc-123-def-456"})
    assert r.headers["X-Request-ID"] == "abc-123-def-456"
    r = await client.get("/api/health/live", headers={"X-Request-ID": "<script>bad</script>"})
    assert r.headers["X-Request-ID"] != "<script>bad</script>"
    assert len(r.headers["X-Request-ID"]) == 32


async def test_docs_disabled_outside_development(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/docs")).status_code == 404
    assert (await client.get("/api/openapi.json")).status_code == 404


async def test_docs_enabled_in_development(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path, env=Environment.development))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.get("/api/openapi.json")).status_code == 200


async def test_hsts_in_production(tmp_path: Path) -> None:
    app = create_app(
        make_settings(
            tmp_path, env=Environment.production, cookie_secure=True, allowed_origins=["https://x.local"]
        )
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/api/health/live")
    assert r.headers["Strict-Transport-Security"].startswith("max-age=")
