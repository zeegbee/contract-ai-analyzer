"""Раздел «Настройки»: системные параметры (Admin/Superadmin, только чтение) и политика хранения документов.

Системные параметры задаются переменными окружения (docs/OPERATIONS.md); секретов, адресов БД и ключей
здесь нет — только явно перечисленные лимиты. Облачные модели и текст предупреждения —
`/api/admin/llm-settings`. Политику хранения (`/api/admin/retention`) видят Admin и Superadmin,
меняет только Superadmin (`app.services.retention`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict

from app.api.deps import AdminAuth, AppSettings, Db
from app.security import policies
from app.services import retention

router = APIRouter(tags=["settings"])


class SystemParameter(BaseModel):
    key: str
    value: int | float | str | bool
    env: str


class SystemSettingsOut(BaseModel):
    groups: dict[str, list[SystemParameter]]
    editable: bool = False


_GROUPS: dict[str, tuple[str, ...]] = {
    "documents": (
        "max_upload_mb",
        "max_pages",
        "max_pending_extractions_per_user",
        "extraction_timeout_seconds",
    ),
    "analysis": (
        "analysis_max_tokens",
        "analysis_max_active_per_user",
        "analysis_default_max_output_tokens",
        "analysis_quote_retry_limit",
    ),
    "exports": ("export_ttl_hours", "max_pending_exports_per_user"),
    "sessions": (
        "session_idle_minutes",
        "session_absolute_hours",
        "login_max_failures_per_email",
        "login_window_minutes",
    ),
    "worker": ("worker_concurrency", "job_lease_seconds"),
}


@router.get("/admin/system-settings")
async def system_settings(auth: AdminAuth, settings: AppSettings) -> SystemSettingsOut:
    _ = auth
    groups = {
        group: [SystemParameter(key=k, value=getattr(settings, k), env=k.upper()) for k in keys]
        for group, keys in _GROUPS.items()
    }
    return SystemSettingsOut(groups=groups)


class RetentionOut(BaseModel):
    # null — автоматическое удаление выключено.
    retention_days: int | None
    updated_at: datetime | None
    updated_by_id: uuid.UUID | None
    # Сколько документов удаляется при текущем сроке (при выключенной политике — null).
    eligible_now: int | None
    # Предпросмотр для срока `preview_days` из запроса.
    preview_days: int | None = None
    preview_count: int | None = None
    can_manage: bool
    min_days: int = retention.MIN_DAYS
    max_days: int = retention.MAX_DAYS


class RetentionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retention_days: int | None
    # Явное подтверждение: включение или сокращение срока сразу удаляет подходящие документы.
    confirm: bool = False


async def _retention_out(db: Db, auth: AdminAuth, preview_days: int | None) -> RetentionOut:
    state = await retention.get_state(db)
    eligible = await retention.count_eligible(db, state.retention_days) if state.retention_days else None
    preview = await retention.count_eligible(db, preview_days) if preview_days else None
    await db.commit()
    return RetentionOut(
        retention_days=state.retention_days,
        updated_at=state.updated_at,
        updated_by_id=state.updated_by_id,
        eligible_now=eligible,
        preview_days=preview_days,
        preview_count=preview,
        can_manage=policies.can_manage_retention(auth.user),
    )


@router.get("/admin/retention")
async def get_retention(
    auth: AdminAuth,
    db: Db,
    preview_days: Annotated[int | None, Query(ge=retention.MIN_DAYS, le=retention.MAX_DAYS)] = None,
) -> RetentionOut:
    return await _retention_out(db, auth, preview_days)


@router.put("/admin/retention")
async def put_retention(body: RetentionIn, auth: AdminAuth, db: Db) -> RetentionOut:
    await retention.update(db, auth.user, auth.ip, days=body.retention_days, confirm=body.confirm)
    return await _retention_out(db, auth, None)
