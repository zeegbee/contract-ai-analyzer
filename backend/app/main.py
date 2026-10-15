"""Фабрика FastAPI-приложения."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from app.api import health
from app.config import Settings, get_settings
from app.db.session import create_engine, create_session_factory
from app.errors import install_error_handlers
from app.logging import configure_logging, get_logger
from app.middleware import RequestContextMiddleware
from app.storage import LocalStorage

log = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    engine = create_engine(settings)
    storage = LocalStorage(settings.storage_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        storage.ensure_layout()
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
    app.state.session_factory = create_session_factory(engine)
    app.state.storage = storage

    install_error_handlers(app)
    app.add_middleware(RequestContextMiddleware, hsts=settings.is_production)

    api = APIRouter(prefix="/api")
    api.include_router(health.router)
    app.include_router(api)
    return app
