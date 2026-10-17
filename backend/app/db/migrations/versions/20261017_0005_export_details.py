"""Экспорт отчётов: код ошибки и размер файла.

Неразрушающая миграция: nullable-колонки (таблица exports до этапа 07 не заполнялась).

Revision ID: 0005_export_details
Revises: 0004_analysis_pipeline
Create Date: 2026-10-17 18:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_export_details"
down_revision: str | None = "0004_analysis_pipeline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("exports", sa.Column("error_code", sa.String(length=64), nullable=True))
    op.add_column("exports", sa.Column("size_bytes", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("exports", "size_bytes")
    op.drop_column("exports", "error_code")
