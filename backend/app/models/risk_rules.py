"""Правила рисков организации (этап 11).

Правило уточняет, что искать в своей категории; текст выбранных правил копируется в snapshot анализа
(анализ не зависит от последующих изменений правила). Правила не удаляются — архивируются.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, Sequence, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPk, str_enum, tz_datetime
from app.models.enums import RiskCategory, RiskRuleKind, RiskSeverity

# Номер для кода `RR-001`…: последовательность не откатывается, коды не переиспользуются.
RISK_RULE_CODE_SEQ = Sequence("risk_rule_code_seq")


class RiskRule(UUIDPk, Timestamps, Base):
    __tablename__ = "risk_rules"

    code: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[RiskCategory] = mapped_column(str_enum(RiskCategory, "risk_category"), nullable=False)
    kind: Mapped[RiskRuleKind] = mapped_column(str_enum(RiskRuleKind, "risk_rule_kind"), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    signs: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Рекомендуемый уровень; модель может отступить от него с объяснением.
    severity: Mapped[RiskSeverity | None] = mapped_column(
        str_enum(RiskSeverity, "risk_severity"), nullable=True
    )
    recommendation: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    default_selected: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    archived_at: Mapped[datetime | None] = tz_datetime(nullable=True)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012
