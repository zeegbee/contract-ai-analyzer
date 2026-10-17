"""Журнал запусков резервного копирования (этап 08A).

Неразрушающая миграция: новая таблица `backup_runs` (метаданные запусков, которые сообщает скрипт
`deploy/backup.sh` через `python -m app.cli backup-report`).

Revision ID: 0007_backup_runs
Revises: 0006_audit_log_append_only
Create Date: 2026-10-17 22:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_backup_runs"
down_revision: str | None = "0006_audit_log_append_only"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "backup_runs",
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "success",
                "failed",
                "interrupted",
                name="backup_run_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "trigger",
            sa.Enum(
                "scheduled",
                "requested",
                "manual",
                name="backup_trigger",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("target", sa.String(length=16), nullable=False),
        sa.Column("encrypted", sa.Boolean(), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("uploaded", sa.Boolean(), nullable=True),
        sa.Column("verified", sa.Boolean(), nullable=True),
        sa.Column("pruned_local", sa.Integer(), nullable=True),
        sa.Column("pruned_remote", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('running', 'success', 'failed', 'interrupted')",
            name=op.f("ck_backup_runs_backup_run_status"),
        ),
        sa.CheckConstraint(
            "trigger IN ('scheduled', 'requested', 'manual')", name=op.f("ck_backup_runs_backup_trigger")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backup_runs")),
    )
    op.create_index("ix_backup_runs_started", "backup_runs", ["started_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_backup_runs_started", table_name="backup_runs")
    op.drop_table("backup_runs")
