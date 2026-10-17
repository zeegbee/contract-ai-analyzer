"""Задачи, экспорт, аудит, настройки, ревизии конфигурации, heartbeat worker'ов."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAt, Timestamps, UUIDPk, str_enum, tz_datetime
from app.models.enums import BackupRunStatus, BackupTrigger, ExportFormat, ExportStatus, JobStatus


class Job(UUIDPk, Timestamps, Base):
    __tablename__ = "jobs"

    type: Mapped[str] = mapped_column(String(64), nullable=False)
    # Только идентификаторы, без содержимого документов.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status: Mapped[JobStatus] = mapped_column(
        str_enum(JobStatus, "job_status"), nullable=False, server_default=JobStatus.queued.value
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))
    run_after: Mapped[datetime] = tz_datetime(nullable=False, server_default=text("now()"))
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = tz_datetime(nullable=True)
    heartbeat_at: Mapped[datetime | None] = tz_datetime(nullable=True)
    started_at: Mapped[datetime | None] = tz_datetime(nullable=True)
    finished_at: Mapped[datetime | None] = tz_datetime(nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dedup_key: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (
        Index(
            "ix_jobs_queue",
            "priority",
            "run_after",
            postgresql_where=text("status = 'queued'"),
        ),
        Index("ix_jobs_running_lease", "lease_expires_at", postgresql_where=text("status = 'running'")),
        # Дедупликация только среди активных задач
        Index(
            "uq_jobs_active_dedup_key",
            "dedup_key",
            unique=True,
            postgresql_where=text("dedup_key IS NOT NULL AND status IN ('queued', 'running')"),
        ),
    )


class Export(UUIDPk, Timestamps, Base):
    __tablename__ = "exports"

    analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analyses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    format: Mapped[ExportFormat] = mapped_column(str_enum(ExportFormat, "export_format"), nullable=False)
    status: Mapped[ExportStatus] = mapped_column(
        str_enum(ExportStatus, "export_status"), nullable=False, server_default=ExportStatus.queued.value
    )
    storage_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    expires_at: Mapped[datetime | None] = tz_datetime(nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class AuditLog(Base):
    """Append-only журнал. Приложение не изменяет и не удаляет записи."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    created_at: Mapped[datetime] = tz_datetime(nullable=False, server_default=text("now()"))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))

    __table_args__ = (
        Index("ix_audit_log_created_at", "created_at"),
        Index("ix_audit_log_actor_created", "actor_user_id", "created_at"),
        Index("ix_audit_log_action_created", "action", "created_at"),
    )


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = tz_datetime(nullable=False, server_default=text("now()"))
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ConfigRevision(UUIDPk, CreatedAt, Base):
    __tablename__ = "config_revisions"

    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    # Секреты в diff заменяются на "***" до записи.
    diff: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    changed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (UniqueConstraint("entity_type", "entity_id", "revision"),)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    started_at: Mapped[datetime] = tz_datetime(nullable=False, server_default=text("now()"))
    last_seen_at: Mapped[datetime] = tz_datetime(nullable=False, server_default=text("now()"))
    version: Mapped[str] = mapped_column(String(32), nullable=False)


class BackupRun(UUIDPk, Base):
    """Запуск резервного копирования: сообщает скрипт на хосте (`python -m app.cli backup-report`).

    Только метаданные: имя архива, размер, результат выгрузки и проверки, код ошибки.
    """

    __tablename__ = "backup_runs"

    started_at: Mapped[datetime] = tz_datetime(nullable=False, server_default=text("now()"))
    finished_at: Mapped[datetime | None] = tz_datetime(nullable=True)
    status: Mapped[BackupRunStatus] = mapped_column(
        str_enum(BackupRunStatus, "backup_run_status"), nullable=False
    )
    trigger: Mapped[BackupTrigger] = mapped_column(str_enum(BackupTrigger, "backup_trigger"), nullable=False)
    target: Mapped[str] = mapped_column(String(16), nullable=False)
    encrypted: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    uploaded: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    pruned_local: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pruned_remote: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (Index("ix_backup_runs_started", "started_at"),)
