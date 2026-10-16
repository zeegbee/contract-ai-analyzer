"""Провайдеры LLM и каталог моделей."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, Integer, LargeBinary, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPk, str_enum, tz_datetime
from app.models.enums import ProviderType, StructuredOutputSupport


class ProviderConfig(UUIDPk, Timestamps, Base):
    __tablename__ = "provider_configs"

    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    type: Mapped[ProviderType] = mapped_column(str_enum(ProviderType, "provider_type"), nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    is_cloud: Mapped[bool] = mapped_column(Boolean, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # Секрет хранится только зашифрованным; ключ шифрования — вне БД.
    secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    secret_key_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    secret_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    allow_private_network: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    timeout_s: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("120"))
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("2"))
    extra_headers: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    last_check_at: Mapped[datetime | None] = tz_datetime(nullable=True)
    last_check_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_check_error_safe: Mapped[str | None] = mapped_column(String(500), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012


class ModelConfig(UUIDPk, Timestamps, Base):
    __tablename__ = "model_configs"

    provider_config_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("provider_configs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    external_model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    purpose: Mapped[list[str]] = mapped_column(ARRAY(String(32)), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    allowed_roles: Mapped[list[str]] = mapped_column(ARRAY(String(32)), nullable=False)
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    generation_params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    structured_output: Mapped[StructuredOutputSupport] = mapped_column(
        str_enum(StructuredOutputSupport, "structured_output_support"),
        nullable=False,
        server_default=StructuredOutputSupport.unknown.value,
    )
    # {"prompt_per_mtok", "completion_per_mtok", "currency", "source", "fetched_at"}
    # NULL = цена неизвестна; никогда не заполняется предположениями.
    pricing: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Сведения, полученные от провайдера при импорте/проверке (контекст, цены, признаки), с датой.
    provider_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    last_tested_at: Mapped[datetime | None] = tz_datetime(nullable=True)
    last_test_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_test_error_safe: Mapped[str | None] = mapped_column(String(500), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012
    __table_args__ = (
        Index("uq_model_configs_provider_external", "provider_config_id", "external_model_id", unique=True),
        # Не более одной модели по умолчанию (для выбора в форме анализа).
        Index(
            "uq_model_configs_single_default", "is_default", unique=True, postgresql_where=text("is_default")
        ),
    )
