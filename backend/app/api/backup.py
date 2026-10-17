"""Резервное копирование в разделе «Настройки» (этап 08A).

Admin и Superadmin видят настройки, состояние планировщика и журнал запусков; менять настройки
и запрашивать внеочередной бэкап может только Superadmin. Сам бэкап выполняет скрипт на сервере
(`deploy/backup.sh`); параметры хранилища и ключ шифрования задаются только в `.env` сервера.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AdminAuth, AppSettings, Db
from app.models.system import BackupRun
from app.security import policies
from app.services import backup as svc

router = APIRouter(tags=["backup"])


class BackupConfigOut(BaseModel):
    schedule_enabled: bool | None
    schedule: str | None
    timezone: str | None
    target: str | None
    keep_local: int | None
    keep_remote_daily: int | None
    keep_remote_weekly: int | None
    keep_remote_monthly: int | None
    remote_prune: str | None


class ProblemOut(BaseModel):
    code: str
    message: str


class BackupRunOut(BaseModel):
    id: uuid.UUID
    started_at: datetime
    finished_at: datetime | None
    status: str
    trigger: str
    target: str
    encrypted: bool | None
    name: str | None
    size_bytes: int | None
    uploaded: bool | None
    verified: bool | None
    pruned_local: int | None
    pruned_remote: int | None
    error_code: str | None


class BackupStateOut(BaseModel):
    config: BackupConfigOut
    # Откуда значение: ui — задано в интерфейсе, env — из .env, default — вычислено по цели.
    sources: dict[str, str]
    env: BackupConfigOut
    server: dict[str, Any]
    problems: list[ProblemOut]
    previous_run_at: datetime | None
    next_run_at: datetime | None
    scheduler_seen_at: datetime | None
    requested_at: datetime | None
    last_success_at: datetime | None
    overdue: bool
    runs: list[BackupRunOut]
    can_manage: bool


class BackupSettingsIn(BaseModel):
    """Изменяемые в UI параметры; не переданные — без изменений; `reset` — вернуть значение из .env."""

    model_config = ConfigDict(extra="forbid")

    schedule_enabled: bool | None = None
    schedule: str | None = Field(default=None, max_length=100)
    timezone: str | None = Field(default=None, max_length=64)
    target: str | None = Field(default=None, max_length=16)
    keep_local: int | None = None
    keep_remote_daily: int | None = None
    keep_remote_weekly: int | None = None
    keep_remote_monthly: int | None = None
    remote_prune: str | None = Field(default=None, max_length=16)
    reset: list[str] = Field(default_factory=list, max_length=len(svc.EDITABLE))


def _run_out(run: BackupRun) -> BackupRunOut:
    return BackupRunOut(
        id=run.id,
        started_at=run.started_at,
        finished_at=run.finished_at,
        status=run.status.value,
        trigger=run.trigger.value,
        target=run.target,
        encrypted=run.encrypted,
        name=run.name,
        size_bytes=run.size_bytes,
        uploaded=run.uploaded,
        verified=run.verified,
        pruned_local=run.pruned_local,
        pruned_remote=run.pruned_remote,
        error_code=run.error_code,
    )


async def _state(db: Db, settings: AppSettings, auth: AdminAuth) -> BackupStateOut:
    await svc.close_stale_runs(db)
    config = await svc.effective_config(db, settings)
    previous, upcoming = svc.schedule_times(config, datetime.now(UTC))
    request = await svc.pending_request(db)
    last_success = await svc.last_success_at(db)
    state = BackupStateOut(
        config=BackupConfigOut(**config.values),
        sources=config.sources,
        env=BackupConfigOut(**config.env),
        server=svc.server_info(settings),
        problems=[ProblemOut(code=p.code, message=p.message) for p in config.problems],
        previous_run_at=previous,
        next_run_at=upcoming,
        scheduler_seen_at=await svc.scheduler_seen_at(db),
        requested_at=datetime.fromisoformat(request["requested_at"]) if request else None,
        last_success_at=last_success,
        overdue=svc.is_overdue(config, last_success),
        runs=[_run_out(r) for r in await svc.recent_runs(db)],
        can_manage=policies.can_manage_backup(auth.user),
    )
    await db.commit()
    return state


@router.get("/admin/backup")
async def get_backup(auth: AdminAuth, db: Db, settings: AppSettings) -> BackupStateOut:
    return await _state(db, settings, auth)


@router.put("/admin/backup")
async def put_backup(
    body: BackupSettingsIn, auth: AdminAuth, db: Db, settings: AppSettings
) -> BackupStateOut:
    changes = body.model_dump(exclude_unset=True, exclude={"reset"})
    changes = {k: v for k, v in changes.items() if v is not None}
    await svc.update_settings(db, settings, auth.user, ip=auth.ip, changes=changes, reset=body.reset)
    return await _state(db, settings, auth)


@router.post("/admin/backup/run", status_code=202)
async def request_backup(auth: AdminAuth, db: Db, settings: AppSettings) -> BackupStateOut:
    await svc.request_run(db, auth.user, auth.ip)
    return await _state(db, settings, auth)
