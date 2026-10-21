"""Маскировка перед облачным анализом (этап 10).

Неразрушающая миграция: новые таблицы `redaction_previews` (предпросмотры маскировки: зашифрованные
найденные данные и правки пользователя, счётчики без значений) и `analysis_redactions` (зашифрованная
таблица соответствий «метка → значение» анализа). Настройки — строка `system_settings.redaction`
(без изменения схемы).

Revision ID: 0008_redaction
Revises: 0007_backup_runs
Create Date: 2026-10-21 18:45:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_redaction"
down_revision: str | None = "0007_backup_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _mode() -> sa.Enum:
    return sa.Enum(
        "masked",
        "unmasked_by_user",
        name="redaction_mode",
        native_enum=False,
        create_constraint=False,
        length=32,
    )


def upgrade() -> None:
    op.create_table(
        "redaction_previews",
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("version_id", sa.UUID(), nullable=False),
        sa.Column("created_by_id", sa.UUID(), nullable=True),
        sa.Column("settings_revision", sa.Integer(), nullable=False),
        sa.Column("redaction_version", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "scanning",
                "ready",
                "failed",
                "confirmed",
                "expired",
                name="redaction_preview_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            server_default="scanning",
            nullable=False,
        ),
        sa.Column("entities_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("secret_key_id", sa.String(length=32), nullable=True),
        sa.Column(
            "counts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("mode", _mode(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unmasked_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "mode IN ('masked', 'unmasked_by_user')", name=op.f("ck_redaction_previews_redaction_mode")
        ),
        sa.CheckConstraint(
            "status IN ('scanning', 'ready', 'failed', 'confirmed', 'expired')",
            name=op.f("ck_redaction_previews_redaction_preview_status"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_redaction_previews_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_redaction_previews_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["document_versions.id"],
            name=op.f("fk_redaction_previews_version_id_document_versions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_redaction_previews")),
    )
    op.create_index(
        op.f("ix_redaction_previews_created_by_id"), "redaction_previews", ["created_by_id"], unique=False
    )
    op.create_index(
        op.f("ix_redaction_previews_document_id"), "redaction_previews", ["document_id"], unique=False
    )
    op.create_index(
        op.f("ix_redaction_previews_version_id"), "redaction_previews", ["version_id"], unique=False
    )
    op.create_table(
        "analysis_redactions",
        sa.Column("analysis_id", sa.UUID(), nullable=False),
        sa.Column("preview_id", sa.UUID(), nullable=True),
        sa.Column("mode", _mode(), nullable=False),
        sa.Column("unmasked_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mapping_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("secret_key_id", sa.String(length=32), nullable=True),
        sa.Column(
            "counts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("redaction_version", sa.String(length=32), nullable=False),
        sa.Column("settings_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "mode IN ('masked', 'unmasked_by_user')", name=op.f("ck_analysis_redactions_redaction_mode")
        ),
        sa.ForeignKeyConstraint(
            ["analysis_id"],
            ["analyses.id"],
            name=op.f("fk_analysis_redactions_analysis_id_analyses"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["preview_id"],
            ["redaction_previews.id"],
            name=op.f("fk_analysis_redactions_preview_id_redaction_previews"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("analysis_id", name=op.f("pk_analysis_redactions")),
        sa.UniqueConstraint("preview_id", name=op.f("uq_analysis_redactions_preview_id")),
    )


def downgrade() -> None:
    op.drop_table("analysis_redactions")
    op.drop_index(op.f("ix_redaction_previews_version_id"), table_name="redaction_previews")
    op.drop_index(op.f("ix_redaction_previews_document_id"), table_name="redaction_previews")
    op.drop_index(op.f("ix_redaction_previews_created_by_id"), table_name="redaction_previews")
    op.drop_table("redaction_previews")
