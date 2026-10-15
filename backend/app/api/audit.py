"""Журнал аудита — только чтение (Admin, Superadmin)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import AdminAuth, Db
from app.models.system import AuditLog
from app.models.users import User

router = APIRouter(prefix="/admin/audit", tags=["audit"])


class AuditEntryOut(BaseModel):
    id: int
    created_at: datetime
    actor_user_id: uuid.UUID | None
    actor_email: str | None
    action: str
    target_type: str | None
    target_id: str | None
    ip: str | None
    request_id: str | None
    details: dict[str, Any]


class AuditListOut(BaseModel):
    items: list[AuditEntryOut]
    next_cursor: int | None


@router.get("")
async def list_audit(
    auth: AdminAuth,
    db: Db,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before_id: Annotated[int | None, Query(ge=1)] = None,
    action: Annotated[str | None, Query(max_length=64, pattern=r"^[a-z_.]+$")] = None,
    actor_user_id: uuid.UUID | None = None,
    target_id: Annotated[str | None, Query(max_length=64)] = None,
) -> AuditListOut:
    stmt = select(AuditLog, User.email).outerjoin(User, User.id == AuditLog.actor_user_id)
    if before_id is not None:
        stmt = stmt.where(AuditLog.id < before_id)
    if action:
        # «auth.» — все действия группы, «auth.login_failed» — конкретное
        stmt = stmt.where(
            AuditLog.action.startswith(action) if action.endswith(".") else AuditLog.action == action
        )
    if actor_user_id is not None:
        stmt = stmt.where(AuditLog.actor_user_id == actor_user_id)
    if target_id:
        stmt = stmt.where(AuditLog.target_id == target_id)
    stmt = stmt.order_by(AuditLog.id.desc()).limit(limit + 1)
    rows = (await db.execute(stmt)).all()
    items = [
        AuditEntryOut(
            id=entry.id,
            created_at=entry.created_at,
            actor_user_id=entry.actor_user_id,
            actor_email=email,
            action=entry.action,
            target_type=entry.target_type,
            target_id=entry.target_id,
            ip=str(entry.ip) if entry.ip else None,
            request_id=entry.request_id,
            details=entry.details,
        )
        for entry, email in rows[:limit]
    ]
    next_cursor = items[-1].id if len(rows) > limit and items else None
    return AuditListOut(items=items, next_cursor=next_cursor)
