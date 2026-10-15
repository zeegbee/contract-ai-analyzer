"""Liveness и readiness. Не обращаются к внешним LLM-провайдерам."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.db.migrations_state import current_revisions, expected_heads
from app.logging import get_logger

router = APIRouter(prefix="/health", tags=["health"])
log = get_logger(__name__)

_DB_TIMEOUT_S = 3.0


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    state = request.app.state
    checks: dict[str, Any] = {}

    try:
        async with asyncio.timeout(_DB_TIMEOUT_S), state.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            revisions = await current_revisions(conn)
        checks["database"] = "ok"
        checks["migrations"] = "ok" if revisions == expected_heads() else "pending"
    except Exception as exc:
        log.warning("readiness_db_failed", exc_type=type(exc).__name__)
        checks["database"] = "unavailable"
        checks["migrations"] = "unknown"

    checks["storage"] = "ok" if state.storage.check_writable() else "unavailable"

    is_ready = all(v == "ok" for v in checks.values())
    body = {
        "status": "ready" if is_ready else "not_ready",
        "checks": checks,
        "version": state.settings.app_version,
    }
    return JSONResponse(status_code=200 if is_ready else 503, content=body)
