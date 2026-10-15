"""Пользователи, сессии, разрешения и согласия."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import INET, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAt, Timestamps, UUIDPk, str_enum, tz_datetime
from app.models.enums import DocumentPermission, UserRole


class User(UUIDPk, Timestamps, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[UserRole] = mapped_column(str_enum(UserRole, "user_role"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    locked_until: Mapped[datetime | None] = tz_datetime(nullable=True)
    password_changed_at: Mapped[datetime | None] = tz_datetime(nullable=True)
    last_login_at: Mapped[datetime | None] = tz_datetime(nullable=True)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        # email хранится в нижнем регистре; уникальность — по lower() на случай ручных вставок
        Index("uq_users_email_lower", text("lower(email)"), unique=True),
    )


class UserSession(UUIDPk, CreatedAt, Base):
    __tablename__ = "sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    csrf_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    last_seen_at: Mapped[datetime] = tz_datetime(nullable=False, server_default=text("now()"))
    expires_at: Mapped[datetime] = tz_datetime(nullable=False)
    idle_expires_at: Mapped[datetime] = tz_datetime(nullable=False)
    revoked_at: Mapped[datetime | None] = tz_datetime(nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)


class LoginAttempt(UUIDPk, CreatedAt, Base):
    """Скользящее окно для rate limit входа (по IP и по email)."""

    __tablename__ = "login_attempts"

    email_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)

    __table_args__ = (
        Index("ix_login_attempts_email_hash_created_at", "email_hash", "created_at"),
        Index("ix_login_attempts_ip_created_at", "ip", "created_at"),
    )


class DocumentAccess(UUIDPk, CreatedAt, Base):
    __tablename__ = "document_access"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    permission: Mapped[DocumentPermission] = mapped_column(
        str_enum(DocumentPermission, "document_permission"), nullable=False
    )
    granted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (UniqueConstraint("document_id", "user_id"),)


class UserModelPermission(UUIDPk, CreatedAt, Base):
    __tablename__ = "user_model_permissions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    model_config_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_configs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)

    __table_args__ = (UniqueConstraint("user_id", "model_config_id"),)


class ConsentRecord(UUIDPk, Base):
    __tablename__ = "consent_records"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider_config_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("provider_configs.id", ondelete="CASCADE"), nullable=False
    )
    notice_version: Mapped[str] = mapped_column(String(32), nullable=False)
    accepted_at: Mapped[datetime] = tz_datetime(nullable=False, server_default=text("now()"))

    __table_args__ = (UniqueConstraint("user_id", "provider_config_id", "notice_version"),)
