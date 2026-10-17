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
_ACTIVE_SUPERADMIN = text("SELECT EXISTS (SELECT 1 FROM users WHERE role = 'superadmin' AND is_active)")
# Worker пишет heartbeat не реже раза в 15–60 с; старше — worker остановлен или завис.
_WORKER_ALIVE = text(
    "SELECT EXISTS (SELECT 1 FROM worker_heartbeats WHERE last_seen_at > now() - interval '120 seconds')"
)


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    state = request.app.state
    checks: dict[str, Any] = {}
    worker = "unknown"

    try:
        async with asyncio.timeout(_DB_TIMEOUT_S), state.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            revisions = await current_revisions(conn)
            checks["database"] = "ok"
            checks["migrations"] = "ok" if revisions == expected_heads() else "pending"
            if checks["migrations"] == "ok":
                has_admin = (await conn.execute(_ACTIVE_SUPERADMIN)).scalar_one()
                # Без активного Superadmin войти некому: нужен bootstrap (BOOTSTRAP_ADMIN_*).
                checks["accounts"] = "ok" if has_admin else "bootstrap_required"
                worker = "ok" if (await conn.execute(_WORKER_ALIVE)).scalar_one() else "unavailable"
            else:
                checks["accounts"] = "unknown"
    except Exception as exc:
        log.warning("readiness_db_failed", exc_type=type(exc).__name__)
        checks["database"] = "unavailable"
        checks["migrations"] = "unknown"
        checks["accounts"] = "unknown"

    checks["storage"] = "ok" if state.storage.check_writable() else "unavailable"

    is_ready = all(v == "ok" for v in checks.values())
    # Состояние worker'а не влияет на готовность API (запросы обслуживаются), но без него загрузки,
    # анализы и экспорт не выполняются — показываем отдельно для мониторинга и страницы «Состояние».
    checks["worker"] = worker
    body = {
        "status": "ready" if is_ready else "not_ready",
        "checks": checks,
        "version": state.settings.app_version,
    }
    return JSONResponse(status_code=200 if is_ready else 503, content=body)
