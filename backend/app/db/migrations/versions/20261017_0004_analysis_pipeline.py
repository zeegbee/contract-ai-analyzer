"""Конвейер анализа: промежуточные результаты этапов и номер попытки доказательства.

Неразрушающая миграция: добавляет колонки со значениями по умолчанию
(таблицы анализов до этапа 06 не заполнялись).

- `analyses.stage_results` — результаты этапов, не привязанные к чанкам (структура, межраздельный
  проход, детектор prompt injection, отметки о выполненных этапах) — для продолжения после сбоя;
- `risk_evidence.attempt` — 0 для доказательств из анализа, 1 — из повторного запроса цитаты.

Revision ID: 0004_analysis_pipeline
Revises: 0003_model_catalog
Create Date: 2026-10-17 12:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_analysis_pipeline"
down_revision: str | None = "0003_model_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "analyses",
        sa.Column(
            "stage_results",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "risk_evidence",
        sa.Column("attempt", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_index("ix_analyses_created_by_id_status", "analyses", ["created_by_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_analyses_created_by_id_status", table_name="analyses")
    op.drop_column("risk_evidence", "attempt")
    op.drop_column("analyses", "stage_results")
