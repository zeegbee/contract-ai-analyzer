"""DTO аутентификации и управления пользователями."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from app.models.enums import UserRole
from app.security.passwords import MAX_PASSWORD_LENGTH

# Простая проверка формата: локальная часть, @, домен с точкой. Полная валидация RFC 5322 не нужна —
# адрес служит логином, письма система не отправляет.
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s.]{1,63}$")


def normalize_email(value: str) -> str:
    value = value.strip().lower()
    if not _EMAIL_RE.match(value):
        raise ValueError("invalid email")
    return value


Email = Annotated[str, StringConstraints(max_length=320), AfterValidator(normalize_email)]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
# Пароль при входе не валидируется политикой (её проверяют при установке), только ограничивается длина.
PasswordInput = Annotated[str, StringConstraints(min_length=1, max_length=MAX_PASSWORD_LENGTH)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(StrictModel):
    # Формат e-mail при входе не проверяется: ответ на некорректный адрес такой же, как на неверный пароль.
    email: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=320)]
    password: PasswordInput


class ChangePasswordRequest(StrictModel):
    current_password: PasswordInput
    new_password: PasswordInput


class UserCapabilities(BaseModel):
    manage_users: bool
    view_audit_log: bool
    upload_documents: bool
    manage_models: bool
    manage_providers: bool
    manage_llm_settings: bool
    assignable_roles: list[UserRole]


class MeOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    role: UserRole
    must_change_password: bool
    capabilities: UserCapabilities


class SessionOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    ip: str | None
    user_agent: str | None
    current: bool


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    role: UserRole
    is_active: bool
    must_change_password: bool
    failed_login_count: int
    locked_until: datetime | None
    last_login_at: datetime | None
    password_changed_at: datetime | None
    created_at: datetime


class UserDetailOut(UserOut):
    active_sessions: int


class UserListOut(BaseModel):
    items: list[UserOut]
    next_cursor: str | None


class CreateUserRequest(StrictModel):
    email: Email
    display_name: DisplayName
    role: UserRole


class UpdateUserRequest(StrictModel):
    display_name: DisplayName | None = None
    role: UserRole | None = None
    is_active: bool | None = None


class TemporaryPasswordOut(BaseModel):
    """Временный пароль показывается один раз; сервер хранит только его хеш."""

    user: UserOut
    temporary_password: str


class RevokedSessionsOut(BaseModel):
    revoked: int


class ModelPermissionItem(BaseModel):
    model_config_id: uuid.UUID
    display_name: str
    external_model_id: str
    enabled: bool
    allowed_by_role: bool
    override: bool | None = Field(description="null — по роли; true/false — персональное разрешение/запрет")
    effective: bool


class ModelPermissionsOut(BaseModel):
    items: list[ModelPermissionItem]


class ModelPermissionOverride(StrictModel):
    model_config_id: uuid.UUID
    allowed: bool


class ModelPermissionsUpdate(StrictModel):
    overrides: Annotated[list[ModelPermissionOverride], Field(max_length=500)]
