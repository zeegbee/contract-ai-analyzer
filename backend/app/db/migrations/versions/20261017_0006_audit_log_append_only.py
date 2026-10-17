"""Журнал аудита только на добавление (THREAT_MODEL §4.10).

Триггер запрещает UPDATE и DELETE строк `audit_log` для любого подключения, в том числе для
роли приложения: запись нельзя исправить или стереть ни ошибкой кода, ни запросом через приложение.
TRUNCATE (очистка тестовой БД) и удаление таблицы целиком триггер не затрагивает.

Неразрушающая миграция: создаются функция и триггер, данные не меняются.
# non-destructive-sql: только CREATE FUNCTION и CREATE TRIGGER (проверяется test_migration_policy)

Revision ID: 0006_audit_log_append_only
Revises: 0005_export_details
Create Date: 2026-10-17 21:00:00+00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_audit_log_append_only"
down_revision: str | None = "0005_export_details"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION audit_log_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only: % is not allowed', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_append_only
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION audit_log_append_only()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_append_only()")
