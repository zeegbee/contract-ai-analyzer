"""Начальная схема: все сущности из docs/ARCHITECTURE.md §5.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-15 15:44:13.736552+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "failed",
                "cancelled",
                "dead",
                name="job_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("priority", sa.Integer(), server_default=sa.text("100"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("3"), nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("locked_by", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("dedup_key", sa.String(length=200), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'dead')",
            name=op.f("ck_jobs_job_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index(
        "ix_jobs_queue",
        "jobs",
        ["priority", "run_after"],
        unique=False,
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "ix_jobs_running_lease",
        "jobs",
        ["lease_expires_at"],
        unique=False,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index(
        "uq_jobs_active_dedup_key",
        "jobs",
        ["dedup_key"],
        unique=True,
        postgresql_where=sa.text("dedup_key IS NOT NULL AND status IN ('queued', 'running')"),
    )
    op.create_table(
        "login_attempts",
        sa.Column("email_hash", sa.String(length=64), nullable=False),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_login_attempts")),
    )
    op.create_index(
        "ix_login_attempts_email_hash_created_at",
        "login_attempts",
        ["email_hash", "created_at"],
        unique=False,
    )
    op.create_index("ix_login_attempts_ip_created_at", "login_attempts", ["ip", "created_at"], unique=False)
    op.create_table(
        "provider_configs",
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column(
            "type",
            sa.Enum(
                "openrouter",
                "ollama",
                name="provider_type",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("base_url", sa.String(length=500), nullable=False),
        sa.Column("is_cloud", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("secret_key_id", sa.String(length=32), nullable=True),
        sa.Column("secret_last4", sa.String(length=4), nullable=True),
        sa.Column("allow_private_network", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("timeout_s", sa.Integer(), server_default=sa.text("120"), nullable=False),
        sa.Column("max_concurrency", sa.Integer(), server_default=sa.text("2"), nullable=False),
        sa.Column(
            "extra_headers",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("last_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_check_status", sa.String(length=32), nullable=True),
        sa.Column("last_check_error_safe", sa.String(length=500), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "type IN ('openrouter', 'ollama')", name=op.f("ck_provider_configs_provider_type")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_configs")),
        sa.UniqueConstraint("name", name=op.f("uq_provider_configs_name")),
    )
    op.create_table(
        "users",
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "superadmin",
                "admin",
                "analyst",
                "viewer",
                name="user_role",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("must_change_password", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("failed_login_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "role IN ('superadmin', 'admin', 'analyst', 'viewer')", name=op.f("ck_users_user_role")
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"], name=op.f("fk_users_created_by_id_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index("uq_users_email_lower", "users", [sa.literal_column("lower(email)")], unique=True)
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("worker_id", name=op.f("pk_worker_heartbeats")),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=True),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_audit_log_actor_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index("ix_audit_log_action_created", "audit_log", ["action", "created_at"], unique=False)
    op.create_index("ix_audit_log_actor_created", "audit_log", ["actor_user_id", "created_at"], unique=False)
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"], unique=False)
    op.create_table(
        "config_revisions",
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("diff", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("changed_by_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["changed_by_id"],
            ["users.id"],
            name=op.f("fk_config_revisions_changed_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_config_revisions")),
        sa.UniqueConstraint(
            "entity_type",
            "entity_id",
            "revision",
            name=op.f("uq_config_revisions_entity_type_entity_id_revision"),
        ),
    )
    op.create_table(
        "consent_records",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("provider_config_id", sa.UUID(), nullable=False),
        sa.Column("notice_version", sa.String(length=32), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["provider_config_id"],
            ["provider_configs.id"],
            name=op.f("fk_consent_records_provider_config_id_provider_configs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_consent_records_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consent_records")),
        sa.UniqueConstraint(
            "user_id",
            "provider_config_id",
            "notice_version",
            name=op.f("uq_consent_records_user_id_provider_config_id_notice_version"),
        ),
    )
    op.create_table(
        "documents",
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "deleting",
                name="document_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            server_default="active",
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_version_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('active', 'deleting')", name=op.f("ck_documents_document_status")),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_documents_owner_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
    )
    op.create_index(op.f("ix_documents_owner_id"), "documents", ["owner_id"], unique=False)
    op.create_table(
        "model_configs",
        sa.Column("provider_config_id", sa.UUID(), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("external_model_id", sa.String(length=200), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("purpose", postgresql.ARRAY(sa.String(length=32)), nullable=False),
        sa.Column("priority", sa.Integer(), server_default=sa.text("100"), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("allowed_roles", postgresql.ARRAY(sa.String(length=32)), nullable=False),
        sa.Column("context_window", sa.Integer(), nullable=True),
        sa.Column("max_output_tokens", sa.Integer(), nullable=True),
        sa.Column(
            "generation_params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "structured_output",
            sa.Enum(
                "json_schema",
                "json_mode",
                "none",
                "unknown",
                name="structured_output_support",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            server_default="unknown",
            nullable=False,
        ),
        sa.Column("pricing", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_status", sa.String(length=32), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "structured_output IN ('json_schema', 'json_mode', 'none', 'unknown')",
            name=op.f("ck_model_configs_structured_output_support"),
        ),
        sa.ForeignKeyConstraint(
            ["provider_config_id"],
            ["provider_configs.id"],
            name=op.f("fk_model_configs_provider_config_id_provider_configs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_configs")),
    )
    op.create_index(
        op.f("ix_model_configs_provider_config_id"), "model_configs", ["provider_config_id"], unique=False
    )
    op.create_index(
        "uq_model_configs_provider_external",
        "model_configs",
        ["provider_config_id", "external_model_id"],
        unique=True,
    )
    op.create_table(
        "sessions",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=64), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.String(length=256), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_sessions_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_sessions_token_hash")),
    )
    op.create_index(op.f("ix_sessions_user_id"), "sessions", ["user_id"], unique=False)
    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by_id", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_system_settings_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_system_settings")),
    )
    op.create_table(
        "document_access",
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column(
            "permission",
            sa.Enum(
                "view",
                "analyze",
                name="document_permission",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("granted_by_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "permission IN ('view', 'analyze')", name=op.f("ck_document_access_document_permission")
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_access_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_id"],
            ["users.id"],
            name=op.f("fk_document_access_granted_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_document_access_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_access")),
        sa.UniqueConstraint("document_id", "user_id", name=op.f("uq_document_access_document_id_user_id")),
    )
    op.create_index(op.f("ix_document_access_user_id"), "document_access", ["user_id"], unique=False)
    op.create_table(
        "document_versions",
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column(
            "detected_format",
            sa.Enum(
                "pdf",
                "docx",
                "doc",
                name="document_format",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=200), nullable=False),
        sa.Column("converted_storage_key", sa.String(length=200), nullable=True),
        sa.Column("canonical_text_storage_key", sa.String(length=200), nullable=True),
        sa.Column(
            "extraction_status",
            sa.Enum(
                "pending",
                "processing",
                "ready",
                "failed",
                "unsupported_no_text",
                name="extraction_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("extraction_error_code", sa.String(length=64), nullable=True),
        sa.Column("extractor_version", sa.String(length=32), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column(
            "page_source",
            sa.Enum(
                "pdf_native",
                "docx_rendered_breaks",
                "libreoffice_render",
                name="page_source",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("uploaded_by_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "detected_format IN ('pdf', 'docx', 'doc')", name=op.f("ck_document_versions_document_format")
        ),
        sa.CheckConstraint(
            "extraction_status IN ('pending', 'processing', 'ready', 'failed', 'unsupported_no_text')",
            name=op.f("ck_document_versions_extraction_status"),
        ),
        sa.CheckConstraint(
            "page_source IN ('pdf_native', 'docx_rendered_breaks', 'libreoffice_render')",
            name=op.f("ck_document_versions_page_source"),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_versions_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_id"],
            ["users.id"],
            name=op.f("fk_document_versions_uploaded_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_versions")),
        sa.UniqueConstraint(
            "document_id", "version_no", name=op.f("uq_document_versions_document_id_version_no")
        ),
        sa.UniqueConstraint("storage_key", name=op.f("uq_document_versions_storage_key")),
    )
    # Циклическая ссылка documents.current_version_id → document_versions создаётся после обеих таблиц.
    op.create_foreign_key(
        op.f("fk_documents_current_version_id_document_versions"),
        "documents",
        "document_versions",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_table(
        "user_model_permissions",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("model_config_id", sa.UUID(), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["model_config_id"],
            ["model_configs.id"],
            name=op.f("fk_user_model_permissions_model_config_id_model_configs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_model_permissions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_model_permissions")),
        sa.UniqueConstraint(
            "user_id", "model_config_id", name=op.f("uq_user_model_permissions_user_id_model_config_id")
        ),
    )
    op.create_index(
        op.f("ix_user_model_permissions_model_config_id"),
        "user_model_permissions",
        ["model_config_id"],
        unique=False,
    )
    op.create_table(
        "analyses",
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("version_id", sa.UUID(), nullable=False),
        sa.Column("created_by_id", sa.UUID(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "verifying",
                "completed",
                "completed_with_warnings",
                "failed",
                "cancelling",
                "cancelled",
                name="analysis_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            server_default="queued",
            nullable=False,
        ),
        sa.Column(
            "party",
            sa.Enum(
                "party_a",
                "party_b",
                "named",
                "unknown",
                name="analysis_party",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("party_name", sa.String(length=300), nullable=True),
        sa.Column("categories", postgresql.ARRAY(sa.String(length=64)), nullable=False),
        sa.Column(
            "depth",
            sa.Enum(
                "quick",
                "standard",
                "deep",
                name="analysis_depth",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("model_config_id", sa.UUID(), nullable=True),
        sa.Column("config_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cloud_transfer", sa.Boolean(), nullable=False),
        sa.Column("cloud_consent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column(
            "progress",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("coverage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message_safe", sa.String(length=500), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("depth IN ('quick', 'standard', 'deep')", name=op.f("ck_analyses_analysis_depth")),
        sa.CheckConstraint(
            "party IN ('party_a', 'party_b', 'named', 'unknown')", name=op.f("ck_analyses_analysis_party")
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'verifying', 'completed', 'completed_with_warnings', 'failed', 'cancelling', 'cancelled')",
            name=op.f("ck_analyses_analysis_status"),
        ),
        sa.CheckConstraint(
            "NOT cloud_transfer OR cloud_consent_at IS NOT NULL",
            name=op.f("ck_analyses_cloud_transfer_requires_consent"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"], name=op.f("fk_analyses_created_by_id_users"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_analyses_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["model_config_id"],
            ["model_configs.id"],
            name=op.f("fk_analyses_model_config_id_model_configs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["document_versions.id"],
            name=op.f("fk_analyses_version_id_document_versions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analyses")),
        sa.UniqueConstraint(
            "created_by_id", "idempotency_key", name=op.f("uq_analyses_created_by_id_idempotency_key")
        ),
    )
    op.create_index(op.f("ix_analyses_created_by_id"), "analyses", ["created_by_id"], unique=False)
    op.create_index(op.f("ix_analyses_document_id"), "analyses", ["document_id"], unique=False)
    op.create_table(
        "document_fragments",
        sa.Column("version_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("fragment_key", sa.String(length=16), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "heading",
                "paragraph",
                "list_item",
                "table_row",
                "table_cell",
                "footnote",
                "header_footer",
                name="fragment_kind",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_sha256", sa.String(length=64), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("clause_ref", sa.String(length=64), nullable=True),
        sa.Column(
            "heading_path",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("table_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "kind IN ('heading', 'paragraph', 'list_item', 'table_row', 'table_cell', 'footnote', 'header_footer')",
            name=op.f("ck_document_fragments_fragment_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["document_versions.id"],
            name=op.f("fk_document_fragments_version_id_document_versions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_fragments")),
        sa.UniqueConstraint(
            "version_id", "fragment_key", name=op.f("uq_document_fragments_version_id_fragment_key")
        ),
        sa.UniqueConstraint("version_id", "seq", name=op.f("uq_document_fragments_version_id_seq")),
    )
    op.create_index(
        "ix_document_fragments_version_id_page_start",
        "document_fragments",
        ["version_id", "page_start"],
        unique=False,
    )
    op.create_table(
        "document_pages",
        sa.Column("version_id", sa.UUID(), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                "pdf_native",
                "docx_rendered_breaks",
                "libreoffice_render",
                name="page_source",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "source IN ('pdf_native', 'docx_rendered_breaks', 'libreoffice_render')",
            name=op.f("ck_document_pages_page_source"),
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["document_versions.id"],
            name=op.f("fk_document_pages_version_id_document_versions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_pages")),
        sa.UniqueConstraint("version_id", "page_no", name=op.f("uq_document_pages_version_id_page_no")),
    )
    op.create_table(
        "analysis_chunks",
        sa.Column("analysis_id", sa.UUID(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chunk_key", sa.String(length=64), nullable=False),
        sa.Column("fragment_seq_start", sa.Integer(), nullable=False),
        sa.Column("fragment_seq_end", sa.Integer(), nullable=False),
        sa.Column("overlap_from_seq", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "done",
                "failed",
                "skipped",
                name="chunk_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'done', 'failed', 'skipped')",
            name=op.f("ck_analysis_chunks_chunk_status"),
        ),
        sa.ForeignKeyConstraint(
            ["analysis_id"],
            ["analyses.id"],
            name=op.f("fk_analysis_chunks_analysis_id_analyses"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analysis_chunks")),
        sa.UniqueConstraint(
            "analysis_id", "chunk_index", name=op.f("uq_analysis_chunks_analysis_id_chunk_index")
        ),
        sa.UniqueConstraint(
            "analysis_id", "chunk_key", name=op.f("uq_analysis_chunks_analysis_id_chunk_key")
        ),
    )
    op.create_table(
        "analysis_risks",
        sa.Column("analysis_id", sa.UUID(), nullable=False),
        sa.Column("risk_key", sa.String(length=16), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "clause_risk",
                "missing_clause",
                "contradiction",
                name="risk_kind",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column(
            "severity",
            sa.Enum(
                "critical",
                "high",
                "medium",
                "low",
                "informational",
                name="risk_severity",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("consequences", sa.Text(), nullable=False),
        sa.Column(
            "affected_party",
            sa.Enum(
                "party_a",
                "party_b",
                "both",
                "unclear",
                name="affected_party",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("recommendation", sa.Text(), nullable=False),
        sa.Column("suggested_revision", sa.Text(), nullable=True),
        sa.Column("model_confidence", sa.Float(), nullable=True),
        sa.Column(
            "validation_status",
            sa.Enum(
                "pending",
                "verified_match",
                "needs_review",
                "rejected",
                name="validation_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "validation_notes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("search_scope", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "source_chunk_ids", postgresql.ARRAY(sa.UUID()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("dedup_group", sa.String(length=64), nullable=True),
        sa.Column("is_hidden_by_user", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "affected_party IN ('party_a', 'party_b', 'both', 'unclear')",
            name=op.f("ck_analysis_risks_affected_party"),
        ),
        sa.CheckConstraint(
            "kind IN ('clause_risk', 'missing_clause', 'contradiction')",
            name=op.f("ck_analysis_risks_risk_kind"),
        ),
        sa.CheckConstraint(
            "severity IN ('critical', 'high', 'medium', 'low', 'informational')",
            name=op.f("ck_analysis_risks_risk_severity"),
        ),
        sa.CheckConstraint(
            "validation_status IN ('pending', 'verified_match', 'needs_review', 'rejected')",
            name=op.f("ck_analysis_risks_validation_status"),
        ),
        sa.CheckConstraint(
            "model_confidence IS NULL OR (model_confidence >= 0 AND model_confidence <= 1)",
            name=op.f("ck_analysis_risks_model_confidence_range"),
        ),
        sa.ForeignKeyConstraint(
            ["analysis_id"],
            ["analyses.id"],
            name=op.f("fk_analysis_risks_analysis_id_analyses"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analysis_risks")),
        sa.UniqueConstraint("analysis_id", "risk_key", name=op.f("uq_analysis_risks_analysis_id_risk_key")),
    )
    op.create_index(
        "ix_analysis_risks_analysis_id_severity", "analysis_risks", ["analysis_id", "severity"], unique=False
    )
    op.create_table(
        "exports",
        sa.Column("analysis_id", sa.UUID(), nullable=False),
        sa.Column("requested_by_id", sa.UUID(), nullable=True),
        sa.Column(
            "format",
            sa.Enum(
                "pdf",
                "docx",
                "csv",
                "xlsx",
                name="export_format",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "ready",
                "failed",
                "expired",
                name="export_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("storage_key", sa.String(length=200), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("format IN ('pdf', 'docx', 'csv', 'xlsx')", name=op.f("ck_exports_export_format")),
        sa.CheckConstraint(
            "status IN ('queued', 'ready', 'failed', 'expired')", name=op.f("ck_exports_export_status")
        ),
        sa.ForeignKeyConstraint(
            ["analysis_id"], ["analyses.id"], name=op.f("fk_exports_analysis_id_analyses"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_id"],
            ["users.id"],
            name=op.f("fk_exports_requested_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_exports")),
    )
    op.create_index(op.f("ix_exports_analysis_id"), "exports", ["analysis_id"], unique=False)
    op.create_table(
        "risk_evidence",
        sa.Column("risk_id", sa.UUID(), nullable=False),
        sa.Column("fragment_id", sa.UUID(), nullable=True),
        sa.Column("claimed_fragment_key", sa.String(length=32), nullable=False),
        sa.Column(
            "fragment_ids_span", postgresql.ARRAY(sa.UUID()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("quote_normalized", sa.Text(), nullable=True),
        sa.Column(
            "match_status",
            sa.Enum(
                "pending",
                "exact",
                "normalized",
                "not_found",
                "fragment_mismatch",
                "too_short",
                name="match_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("match_char_start", sa.Integer(), nullable=True),
        sa.Column("match_char_end", sa.Integer(), nullable=True),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("clause_ref", sa.String(length=64), nullable=True),
        sa.Column(
            "relevance_status",
            sa.Enum(
                "supports",
                "partially_supports",
                "does_not_support",
                "unchecked",
                name="relevance_status",
                native_enum=False,
                create_constraint=False,
                length=32,
            ),
            server_default="unchecked",
            nullable=False,
        ),
        sa.Column("relevance_rationale", sa.Text(), nullable=True),
        sa.Column("relevance_checked_by", sa.String(length=32), nullable=True),
        sa.Column("verifier_model_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "fragment_id IS NOT NULL OR match_status IN ('fragment_mismatch', 'pending')",
            name=op.f("ck_risk_evidence_fragment_required_for_match"),
        ),
        sa.CheckConstraint(
            "match_status IN ('pending', 'exact', 'normalized', 'not_found', 'fragment_mismatch', 'too_short')",
            name=op.f("ck_risk_evidence_match_status"),
        ),
        sa.CheckConstraint(
            "relevance_status IN ('supports', 'partially_supports', 'does_not_support', 'unchecked')",
            name=op.f("ck_risk_evidence_relevance_status"),
        ),
        sa.ForeignKeyConstraint(
            ["fragment_id"],
            ["document_fragments.id"],
            name=op.f("fk_risk_evidence_fragment_id_document_fragments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["risk_id"],
            ["analysis_risks.id"],
            name=op.f("fk_risk_evidence_risk_id_analysis_risks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_evidence")),
    )
    op.create_index(op.f("ix_risk_evidence_risk_id"), "risk_evidence", ["risk_id"], unique=False)


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_documents_current_version_id_document_versions"), "documents", type_="foreignkey"
    )
    op.drop_index(op.f("ix_risk_evidence_risk_id"), table_name="risk_evidence")
    op.drop_table("risk_evidence")
    op.drop_index(op.f("ix_exports_analysis_id"), table_name="exports")
    op.drop_table("exports")
    op.drop_index("ix_analysis_risks_analysis_id_severity", table_name="analysis_risks")
    op.drop_table("analysis_risks")
    op.drop_table("analysis_chunks")
    op.drop_table("document_pages")
    op.drop_index("ix_document_fragments_version_id_page_start", table_name="document_fragments")
    op.drop_table("document_fragments")
    op.drop_index(op.f("ix_analyses_document_id"), table_name="analyses")
    op.drop_index(op.f("ix_analyses_created_by_id"), table_name="analyses")
    op.drop_table("analyses")
    op.drop_index(op.f("ix_user_model_permissions_model_config_id"), table_name="user_model_permissions")
    op.drop_table("user_model_permissions")
    op.drop_table("document_versions")
    op.drop_index(op.f("ix_document_access_user_id"), table_name="document_access")
    op.drop_table("document_access")
    op.drop_table("system_settings")
    op.drop_index(op.f("ix_sessions_user_id"), table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("uq_model_configs_provider_external", table_name="model_configs")
    op.drop_index(op.f("ix_model_configs_provider_config_id"), table_name="model_configs")
    op.drop_table("model_configs")
    op.drop_index(op.f("ix_documents_owner_id"), table_name="documents")
    op.drop_table("documents")
    op.drop_table("consent_records")
    op.drop_table("config_revisions")
    op.drop_index("ix_audit_log_created_at", table_name="audit_log")
    op.drop_index("ix_audit_log_actor_created", table_name="audit_log")
    op.drop_index("ix_audit_log_action_created", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("worker_heartbeats")
    op.drop_index("uq_users_email_lower", table_name="users")
    op.drop_table("users")
    op.drop_table("provider_configs")
    op.drop_index("ix_login_attempts_ip_created_at", table_name="login_attempts")
    op.drop_index("ix_login_attempts_email_hash_created_at", table_name="login_attempts")
    op.drop_table("login_attempts")
    op.drop_index(
        "uq_jobs_active_dedup_key",
        table_name="jobs",
        postgresql_where=sa.text("dedup_key IS NOT NULL AND status IN ('queued', 'running')"),
    )
    op.drop_index("ix_jobs_running_lease", table_name="jobs", postgresql_where=sa.text("status = 'running'"))
    op.drop_index("ix_jobs_queue", table_name="jobs", postgresql_where=sa.text("status = 'queued'"))
    op.drop_table("jobs")
