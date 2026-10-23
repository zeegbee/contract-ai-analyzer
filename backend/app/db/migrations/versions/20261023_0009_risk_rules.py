"""Правила рисков организации (этап 11).

Неразрушающая миграция: новая таблица `risk_rules`, последовательность кодов `risk_rule_code_seq`
(`RR-001`…) и nullable-колонка `analysis_risks.rule_code` (код правила, по которому найден риск;
текст правила хранится в snapshot анализа).

Revision ID: 0009_risk_rules
Revises: 0008_redaction
Create Date: 2026-10-23 17:25:27+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_risk_rules"
down_revision: str | None = "0008_redaction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=False, length=32)


_CATEGORIES = (
    "payment",
    "liability",
    "penalties",
    "deadlines",
    "renewal",
    "termination",
    "unilateral_changes",
    "warranties",
    "confidentiality",
    "personal_data",
    "intellectual_property",
    "governing_law",
    "force_majeure",
    "acceptance",
    "undefined_terms",
    "contradiction",
    "missing_clause",
    "other",
)
_KINDS = ("clause_risk", "missing_clause", "any")
_SEVERITIES = ("critical", "high", "medium", "low", "informational")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    # non-destructive-sql: только создание последовательности для кодов правил.
    op.execute("CREATE SEQUENCE risk_rule_code_seq")
    op.create_table(
        "risk_rules",
        sa.Column("code", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("category", _enum("risk_category", *_CATEGORIES), nullable=False),
        sa.Column("kind", _enum("risk_rule_kind", *_KINDS), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("signs", sa.Text(), nullable=True),
        sa.Column("severity", _enum("risk_severity", *_SEVERITIES), nullable=True),
        sa.Column("recommendation", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("default_selected", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", sa.UUID(), nullable=True),
        sa.Column("updated_by_id", sa.UUID(), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(_in("category", _CATEGORIES), name=op.f("ck_risk_rules_risk_category")),
        sa.CheckConstraint(_in("kind", _KINDS), name=op.f("ck_risk_rules_risk_rule_kind")),
        sa.CheckConstraint(_in("severity", _SEVERITIES), name=op.f("ck_risk_rules_risk_severity")),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_risk_rules_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_risk_rules_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_rules")),
        sa.UniqueConstraint("code", name=op.f("uq_risk_rules_code")),
    )
    op.add_column("analysis_risks", sa.Column("rule_code", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("analysis_risks", "rule_code")
    op.drop_table("risk_rules")
    op.execute("DROP SEQUENCE risk_rule_code_seq")
