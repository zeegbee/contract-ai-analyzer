"""Тип провайдера ProxyAPI.

Пересоздание CHECK-ограничения `provider_configs.type`: к `openrouter`, `ollama` добавляется
`proxyapi`. Строки таблицы не изменяются и не удаляются.

Revision ID: 0010_proxyapi_provider
Revises: 0009_risk_rules
Create Date: 2026-10-29 12:00:00+00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010_proxyapi_provider"
down_revision: str | None = "0009_risk_rules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_provider_configs_provider_type"


def upgrade() -> None:
    # destructive-approved: владелец 2026-10-29 — пересоздание CHECK с расширенным списком значений,
    # данные не затрагиваются.
    op.drop_constraint(op.f(CONSTRAINT), "provider_configs", type_="check")
    op.create_check_constraint(
        op.f(CONSTRAINT), "provider_configs", "type IN ('openrouter', 'ollama', 'proxyapi')"
    )


def downgrade() -> None:
    # Откат невозможен, пока есть провайдеры ProxyAPI: PostgreSQL отклонит ограничение.
    op.drop_constraint(op.f(CONSTRAINT), "provider_configs", type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), "provider_configs", "type IN ('openrouter', 'ollama')")
