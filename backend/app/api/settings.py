"""Системные параметры для раздела «Настройки» (Admin/Superadmin, только чтение).

Значения задаются переменными окружения (docs/OPERATIONS.md); секретов, адресов БД и ключей здесь нет —
только явно перечисленные лимиты. Облачные модели и текст предупреждения — `/api/admin/llm-settings`.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import AdminAuth, AppSettings

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
