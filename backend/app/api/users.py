"""Управление пользователями (Admin и Superadmin)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import AdminAuth, AuthContext, Db, UserServiceDep
from app.models.enums import UserRole
from app.models.users import User
from app.schemas.users import (
    CreateUserRequest,
    ModelPermissionsOut,
    ModelPermissionsUpdate,
    RevokedSessionsOut,
    TemporaryPasswordOut,
    UpdateUserRequest,
    UserDetailOut,
    UserListOut,
    UserOut,
)
from app.services import users as users_service
from app.services.users import Actor

router = APIRouter(prefix="/users", tags=["users"])


def user_out(user: User) -> UserOut:
    return UserOut.model_validate(user, from_attributes=True)


async def detail_out(db: Db, user: User) -> UserDetailOut:
    active = await users_service.count_active_sessions(db, user.id)
    return UserDetailOut(**user_out(user).model_dump(), active_sessions=active)


def _actor(auth: AuthContext) -> Actor:
    return Actor(user=auth.user, ip=auth.ip)


@router.get("")
async def list_users(
    auth: AdminAuth,
    db: Db,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    role: UserRole | None = None,
    is_active: bool | None = None,
) -> UserListOut:
    rows, next_cursor = await users_service.list_users(
        db, limit=limit, cursor=cursor, query=q, role=role, is_active=is_active
    )
    return UserListOut(items=[user_out(u) for u in rows], next_cursor=next_cursor)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_user(
    body: CreateUserRequest, auth: AdminAuth, db: Db, service: UserServiceDep
) -> TemporaryPasswordOut:
    user, temporary = await service.create_user(
        db, _actor(auth), email=body.email, display_name=body.display_name, role=body.role
    )
    return TemporaryPasswordOut(user=user_out(user), temporary_password=temporary)


@router.get("/{user_id}")
async def get_user(user_id: uuid.UUID, auth: AdminAuth, db: Db) -> UserDetailOut:
    return await detail_out(db, await users_service.get_user(db, user_id))


@router.patch("/{user_id}")
async def update_user(
    user_id: uuid.UUID, body: UpdateUserRequest, auth: AdminAuth, db: Db, service: UserServiceDep
) -> UserDetailOut:
    user = await service.update_user(
        db,
        _actor(auth),
        user_id,
        display_name=body.display_name,
        role=body.role,
        is_active=body.is_active,
    )
    return await detail_out(db, user)


@router.post("/{user_id}/reset-password")
async def reset_password(
    user_id: uuid.UUID, auth: AdminAuth, db: Db, service: UserServiceDep
) -> TemporaryPasswordOut:
    user, temporary = await service.reset_password(db, _actor(auth), user_id)
    return TemporaryPasswordOut(user=user_out(user), temporary_password=temporary)


@router.post("/{user_id}/revoke-sessions")
async def revoke_sessions(
    user_id: uuid.UUID, auth: AdminAuth, db: Db, service: UserServiceDep
) -> RevokedSessionsOut:
    return RevokedSessionsOut(revoked=await service.revoke_sessions(db, _actor(auth), user_id))


@router.post("/{user_id}/unlock")
async def unlock_login(user_id: uuid.UUID, auth: AdminAuth, db: Db, service: UserServiceDep) -> UserDetailOut:
    return await detail_out(db, await service.unlock_login(db, _actor(auth), user_id))


@router.get("/{user_id}/model-permissions")
async def get_model_permissions(user_id: uuid.UUID, auth: AdminAuth, db: Db) -> ModelPermissionsOut:
    user = await users_service.get_user(db, user_id)
    return ModelPermissionsOut(items=await users_service.model_permissions(db, user))


@router.put("/{user_id}/model-permissions")
async def set_model_permissions(
    user_id: uuid.UUID, body: ModelPermissionsUpdate, auth: AdminAuth, db: Db
) -> ModelPermissionsOut:
    user = await users_service.set_model_permissions(db, _actor(auth), user_id, body.overrides)
    return ModelPermissionsOut(items=await users_service.model_permissions(db, user))
