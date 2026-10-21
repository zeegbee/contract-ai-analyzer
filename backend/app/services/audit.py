"""Журнал аудита (append-only).

Запись добавляется в ту же транзакцию, что и само действие, — действие без записи (и наоборот)
не фиксируется. Исключение — неудачный вход: его запись коммитится отдельно, т.к. само действие
не изменяет данных.

В `details` нельзя передавать пароли, токены, ключи, текст и названия договоров. Помимо этого
правила, значения дополнительно проходят через редактирование логов (`app.logging.redact_value`).
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.logging import redact_value
from app.models.system import AuditLog
from app.request_context import get_request_id


class AuditAction(StrEnum):
    # Аутентификация
    login_success = "auth.login_success"
    login_failed = "auth.login_failed"
    logout = "auth.logout"
    password_changed = "auth.password_changed"  # noqa: S105 - код действия, не пароль
    password_change_failed = "auth.password_change_failed"  # noqa: S105
    session_revoked = "auth.session_revoked"
    # Пользователи
    user_bootstrap_created = "user.bootstrap_created"
    user_created = "user.created"
    user_updated = "user.updated"
    user_role_changed = "user.role_changed"
    user_blocked = "user.blocked"
    user_unblocked = "user.unblocked"
    user_password_reset = "user.password_reset"  # noqa: S105
    user_sessions_revoked = "user.sessions_revoked"
    user_login_unlocked = "user.login_unlocked"
    user_model_permissions_changed = "user.model_permissions_changed"
    # Модели и провайдеры (этап 05)
    model_created = "model.created"
    model_updated = "model.updated"
    model_deleted = "model.deleted"
    model_tested = "model.tested"
    models_imported = "model.imported"
    provider_created = "provider.created"
    provider_updated = "provider.updated"
    provider_deleted = "provider.deleted"
    provider_checked = "provider.checked"
    settings_updated = "settings.updated"
    cloud_consent_recorded = "llm.cloud_consent_recorded"
    # Документы, анализы, экспорт (этапы 04–07)
    document_uploaded = "document.uploaded"
    document_deleted = "document.deleted"
    document_access_granted = "document.access_granted"
    document_access_revoked = "document.access_revoked"
    document_accessed_by_superadmin = "document.accessed_by_superadmin"
    document_file_downloaded = "document.file_downloaded"
    document_extraction_retried = "document.extraction_retried"
    analysis_started = "analysis.started"
    analysis_cancelled = "analysis.cancelled"
    analysis_retried = "analysis.retried"
    export_created = "export.created"
    export_downloaded = "export.downloaded"
    # Маскировка перед облачным анализом (этап 10)
    redaction_settings_updated = "redaction.settings_updated"
    redaction_unmasked_allowed = "redaction.unmasked_allowed"
    redaction_unmasked_disallowed = "redaction.unmasked_disallowed"
    redaction_preview_confirmed = "redaction.preview_confirmed"
    redaction_sent_unmasked = "redaction.sent_unmasked"
    # Резервное копирование (этап 08A)
    backup_requested = "backup.requested"
    backup_completed = "backup.completed"
    backup_failed = "backup.failed"


def _safe_details(details: dict[str, Any] | None) -> dict[str, Any]:
    if not details:
        return {}
    return {str(k): redact_value(str(k), v) for k, v in details.items()}


def record(
    session: AsyncSession,
    action: AuditAction,
    *,
    actor_id: uuid.UUID | None,
    target_type: str | None = None,
    target_id: uuid.UUID | str | None = None,
    ip: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    """Добавляет запись в текущую транзакцию (commit выполняет вызывающий код)."""
    entry = AuditLog(
        actor_user_id=actor_id,
        action=action.value,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        ip=ip,
        request_id=get_request_id(),
        details=_safe_details(details),
    )
    session.add(entry)
    return entry
