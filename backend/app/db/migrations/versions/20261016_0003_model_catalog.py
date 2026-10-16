"""Каталог моделей: сведения от провайдера, ошибка последней проверки, одна модель по умолчанию.

Неразрушающая миграция: добавляет nullable-колонки и частичный уникальный индекс
(таблица model_configs до этапа 05 не заполнялась).

Revision ID: 0003_model_catalog
Revises: 0002_upload_idempotency
Create Date: 2026-10-16 18:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_model_catalog"
down_revision: str | None = "0002_upload_idempotency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model_configs",
        sa.Column("provider_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("model_configs", sa.Column("last_test_error_safe", sa.String(length=500), nullable=True))
    op.create_index(
        "uq_model_configs_single_default",
        "model_configs",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_model_configs_single_default", table_name="model_configs", postgresql_where=sa.text("is_default")
    )
    op.drop_column("model_configs", "last_test_error_safe")
    op.drop_column("model_configs", "provider_metadata")
