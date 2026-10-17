"""Фабрика FastAPI-приложения."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from app.api import analyses, audit, auth, catalog, documents, health, resources, users
from app.config import Settings, get_settings
from app.db.session import create_engine, create_session_factory
from app.errors import install_error_handlers
from app.llm.factory import ProviderFactory
from app.logging import configure_logging, get_logger
from app.middleware import RequestContextMiddleware
from app.security.passwords import PRODUCTION_PARAMS, TEST_PARAMS, PasswordService
from app.security.rate_limit import LoginRateLimiter
from app.security.secretbox import SecretBox
from app.security.sessions import SessionManager
from app.services.auth import AuthService
from app.services.bootstrap import log_result, run_bootstrap
from app.services.users import UserService
from app.storage import LocalStorage

log = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    storage = LocalStorage(settings.storage_dir)
    passwords = PasswordService(TEST_PARAMS if settings.is_test else PRODUCTION_PARAMS)
    sessions = SessionManager(settings)
    login_limiter = LoginRateLimiter(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        storage.ensure_layout()
        if settings.bootstrap_enabled:
            await _startup_bootstrap()
        log.info("app_started", env=settings.env.value, version=settings.app_version)
        yield
        await engine.dispose()
        log.info("app_stopped")

    app = FastAPI(
        title="Contract AI Analyzer API",
        version=settings.app_version,
        debug=False,
        lifespan=lifespan,
        docs_url="/api/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.docs_enabled else None,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.storage = storage
    app.state.passwords = passwords
    app.state.sessions = sessions
    app.state.login_limiter = login_limiter
    app.state.auth_service = AuthService(passwords, sessions, login_limiter)
    app.state.user_service = UserService(passwords, sessions, login_limiter)
    app.state.secret_box = SecretBox(settings.secrets_encryption_keys)
    app.state.llm_factory = ProviderFactory(app.state.secret_box)

    async def _startup_bootstrap() -> None:
        # Ошибка (например, миграции ещё не применены) не мешает старту: readiness покажет состояние.
        try:
            async with session_factory() as db:
                result = await run_bootstrap(db, settings, passwords)
            log_result(result, settings)
        except Exception as exc:
            log.error("bootstrap_error", exc_type=type(exc).__name__)

    install_error_handlers(app)
    app.add_middleware(RequestContextMiddleware, hsts=settings.is_production)

    api = APIRouter(prefix="/api")
    api.include_router(health.router)
    api.include_router(auth.router)
    api.include_router(users.router)
    api.include_router(audit.router)
    api.include_router(documents.router)
    api.include_router(analyses.router)
    api.include_router(resources.router)
    api.include_router(catalog.router)
    app.include_router(api)
    return app
