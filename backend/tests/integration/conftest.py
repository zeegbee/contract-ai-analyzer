"""Фикстуры для API-тестов на реальном PostgreSQL."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx._client import ClientState
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.main import create_app
from app.models.enums import UserRole
from app.models.users import User
from app.security.passwords import TEST_PARAMS, PasswordService
from tests.conftest import make_settings

ORIGIN = "http://localhost:8080"
DEFAULT_PASSWORD = "Granite-Velvet-Comet-81"

_PASSWORDS = PasswordService(TEST_PARAMS)

# Все таблицы, которые затрагивают тесты этапа 03 (CASCADE очищает зависимые).
_TRUNCATE = text(
    "TRUNCATE users, sessions, login_attempts, audit_log, system_settings, documents, "
    "provider_configs, jobs, worker_heartbeats CASCADE"
)


class ApiClient(httpx.AsyncClient):
    """Клиент, который можно использовать в `async with` и после первого запроса (например, после login)."""

    async def __aenter__(self) -> ApiClient:
        if self._state is ClientState.OPENED:
            return self
        await super().__aenter__()
        return self


@dataclass
class ApiEnv:
    app: FastAPI
    settings: Settings
    factory: async_sessionmaker[AsyncSession]

    def client(self, *, origin: str | None = ORIGIN) -> ApiClient:
        headers = {"Origin": origin} if origin else {}
        transport = httpx.ASGITransport(app=self.app, client=("203.0.113.10", 50000))
        return ApiClient(transport=transport, base_url=ORIGIN, headers=headers)

    async def create_user(
        self,
        role: UserRole,
        *,
        email: str | None = None,
        password: str = DEFAULT_PASSWORD,
        must_change_password: bool = False,
        is_active: bool = True,
        name: str | None = None,
    ) -> User:
        user = User(
            id=uuid.uuid4(),
            email=email or f"{role.value}-{uuid.uuid4().hex[:8]}@example.org",
            display_name=name or f"Тест {role.value}",
            password_hash=_PASSWORDS.hash(password),
            role=role,
            is_active=is_active,
            must_change_password=must_change_password,
        )
        async with self.factory() as db:
            db.add(user)
            await db.commit()
        return user

    async def login(
        self, user: User, password: str = DEFAULT_PASSWORD, client: ApiClient | None = None
    ) -> ApiClient:
        """Возвращает клиента с cookie сессии и заголовком X-CSRF-Token."""
        client = client or self.client()
        r = await client.post("/api/auth/login", json={"email": user.email, "password": password})
        assert r.status_code == 200, r.text
        client.headers["X-CSRF-Token"] = client.cookies["cai_csrf"]
        return client

    async def execute(self, sql: str, **params: Any) -> None:
        async with self.factory() as db:
            await db.execute(text(sql), params)
            await db.commit()

    async def rows(self, sql: str, **params: Any) -> list[Any]:
        async with self.factory() as db:
            return list((await db.execute(text(sql), params)).all())

    async def scalar(self, sql: str, **params: Any) -> Any:
        async with self.factory() as db:
            return (await db.execute(text(sql), params)).scalar()


def creds(email: str, password: str) -> dict[str, str]:
    return {"email": email, "password": password}


@pytest.fixture
async def api(tmp_path: Any, pg_database_url: str) -> AsyncIterator[ApiEnv]:
    settings = make_settings(
        tmp_path,
        database_url=pg_database_url,
        login_delay_step_ms=0,
        bootstrap_enabled=False,
    )
    app = create_app(settings)
    factory: async_sessionmaker[AsyncSession] = app.state.session_factory
    async with factory() as db:
        await db.execute(_TRUNCATE)
        await db.commit()
    yield ApiEnv(app=app, settings=settings, factory=factory)
    await app.state.engine.dispose()
