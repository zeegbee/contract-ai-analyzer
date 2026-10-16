"""Idempotency-Key загрузки документа.

Неразрушающая миграция: добавляет nullable-колонку и частичный уникальный индекс.

Revision ID: 0002_upload_idempotency
Revises: 0001_initial
Create Date: 2026-10-16 09:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_upload_idempotency"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("upload_idempotency_key", sa.String(length=128), nullable=True))
    op.create_index(
        "uq_documents_owner_upload_idempotency_key",
        "documents",
        ["owner_id", "upload_idempotency_key"],
        unique=True,
        postgresql_where=sa.text("upload_idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_documents_owner_upload_idempotency_key",
        table_name="documents",
        postgresql_where=sa.text("upload_idempotency_key IS NOT NULL"),
    )
    op.drop_column("documents", "upload_idempotency_key")
