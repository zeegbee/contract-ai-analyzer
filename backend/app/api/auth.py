"""Вход, выход, текущий пользователь, смена пароля, собственные сессии."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request, Response, status

from app.api.deps import (
    AppSettings,
    AuthPasswordChange,
    AuthServiceDep,
    Db,
    Sessions,
    client_ip,
    ensure_origin,
)
from app.errors import NotFoundError
from app.models.users import User, UserSession
from app.schemas.users import (
    ChangePasswordRequest,
    LoginRequest,
    MeOut,
    RevokedSessionsOut,
    SessionOut,
    UserCapabilities,
)
from app.security import policies
from app.security.sessions import RevokeReason
from app.services.audit import AuditAction, record

router = APIRouter(prefix="/auth", tags=["auth"])


def me_out(user: User) -> MeOut:
    return MeOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        must_change_password=user.must_change_password,
        capabilities=UserCapabilities(
            manage_users=policies.is_admin(user),
            view_audit_log=policies.can_view_audit_log(user),
            upload_documents=policies.can_upload_documents(user),
            assignable_roles=sorted(policies.assignable_roles(user)),
        ),
    )


def session_out(record_: UserSession, current_id: uuid.UUID) -> SessionOut:
    return SessionOut(
        id=record_.id,
        created_at=record_.created_at,
        last_seen_at=record_.last_seen_at,
        expires_at=min(record_.expires_at, record_.idle_expires_at),
        ip=str(record_.ip) if record_.ip else None,
        user_agent=record_.user_agent,
        current=record_.id == current_id,
    )


@router.post("/login")
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Db,
    settings: AppSettings,
    service: AuthServiceDep,
    sessions: Sessions,
) -> MeOut:
    # Сессии ещё нет, поэтому от login CSRF защищает проверка Origin.
    ensure_origin(request, settings)
    user, issued = await service.login(
        db,
        email=body.email,
        password=body.password,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    sessions.set_cookies(response, issued)
    return me_out(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(auth: AuthPasswordChange, response: Response, db: Db, sessions: Sessions) -> None:
    await sessions.revoke(db, auth.session.id, RevokeReason.logout)
    record(
        db, AuditAction.logout, actor_id=auth.user.id, target_type="user", target_id=auth.user.id, ip=auth.ip
    )
    await db.commit()
    sessions.clear_cookies(response)


@router.get("/me")
async def me(auth: AuthPasswordChange) -> MeOut:
    return me_out(auth.user)


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    response: Response,
    auth: AuthPasswordChange,
    db: Db,
    service: AuthServiceDep,
    sessions: Sessions,
) -> MeOut:
    issued = await service.change_password(
        db,
        auth.user,
        current_password=body.current_password,
        new_password=body.new_password,
        ip=auth.ip,
        user_agent=request.headers.get("user-agent"),
    )
    sessions.set_cookies(response, issued)
    return me_out(auth.user)


@router.get("/sessions")
async def my_sessions(auth: AuthPasswordChange, db: Db, sessions: Sessions) -> list[SessionOut]:
    return [session_out(s, auth.session.id) for s in await sessions.active_sessions(db, auth.user.id)]


@router.post("/sessions/{session_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_my_session(
    session_id: uuid.UUID, auth: AuthPasswordChange, response: Response, db: Db, sessions: Sessions
) -> None:
    # Только собственные сессии: чужая и несуществующая неразличимы.
    owned = {s.id for s in await sessions.active_sessions(db, auth.user.id)}
    if session_id not in owned:
        raise NotFoundError("Сессия не найдена", code="session_not_found")
    await sessions.revoke(db, session_id, RevokeReason.revoked_by_user)
    record(
        db,
        AuditAction.session_revoked,
        actor_id=auth.user.id,
        target_type="session",
        target_id=session_id,
        ip=auth.ip,
    )
    await db.commit()
    if session_id == auth.session.id:
        sessions.clear_cookies(response)


@router.post("/sessions/revoke-others")
async def revoke_other_sessions(auth: AuthPasswordChange, db: Db, sessions: Sessions) -> RevokedSessionsOut:
    revoked = await sessions.revoke_all(
        db, auth.user.id, RevokeReason.revoked_by_user, except_id=auth.session.id
    )
    record(
        db,
        AuditAction.session_revoked,
        actor_id=auth.user.id,
        target_type="user",
        target_id=auth.user.id,
        ip=auth.ip,
        details={"revoked_sessions": revoked, "scope": "others"},
    )
    await db.commit()
    return RevokedSessionsOut(revoked=revoked)
