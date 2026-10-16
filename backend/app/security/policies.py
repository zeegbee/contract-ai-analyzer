"""Политики доступа (RBAC). Единственное место, где описано, кто что может.

Эндпоинты обязаны вызывать эти функции; UI лишь отражает права и защитой не является.
Доступ к конкретным документам/анализам/экспортам проверяется в `app.security.scoped`
условием в SQL-запросе.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from app.errors import PermissionDenied
from app.models.enums import DocumentPermission, UserRole

ADMIN_ROLES = frozenset({UserRole.superadmin, UserRole.admin})
# Роли, которые могут загружать документы и запускать анализ.
CONTRIBUTOR_ROLES = frozenset({UserRole.superadmin, UserRole.admin, UserRole.analyst})


class Actor(Protocol):
    @property
    def id(self) -> object: ...
    @property
    def role(self) -> UserRole: ...


def is_admin(actor: Actor) -> bool:
    return actor.role in ADMIN_ROLES


def is_superadmin(actor: Actor) -> bool:
    return actor.role is UserRole.superadmin


def require_roles(actor: Actor, roles: Iterable[UserRole]) -> None:
    if actor.role not in set(roles):
        raise PermissionDenied()


# ------------------------------------------------------------------ управление пользователями


def assignable_roles(actor: Actor) -> frozenset[UserRole]:
    """Роли, которые actor может назначать (при создании и изменении пользователей)."""
    if actor.role is UserRole.superadmin:
        return frozenset(UserRole)
    if actor.role is UserRole.admin:
        return frozenset({UserRole.admin, UserRole.analyst, UserRole.viewer})
    return frozenset()


def ensure_can_manage_users(actor: Actor) -> None:
    if not is_admin(actor):
        raise PermissionDenied()


def ensure_can_modify_user(actor: Actor, target: Actor) -> None:
    """Изменение чужой учётной записи: роль, блокировка, сброс пароля, сессии, модели."""
    ensure_can_manage_users(actor)
    if target.role is UserRole.superadmin and actor.role is not UserRole.superadmin:
        raise PermissionDenied(
            "Учётные записи Superadmin может изменять только Superadmin", code="superadmin_protected"
        )


def ensure_can_assign_role(actor: Actor, target: Actor, new_role: UserRole) -> None:
    ensure_can_modify_user(actor, target)
    if actor.id == target.id:
        raise PermissionDenied("Нельзя изменить собственную роль", code="cannot_change_own_role")
    if new_role not in assignable_roles(actor):
        raise PermissionDenied("Недостаточно прав для назначения этой роли", code="role_not_assignable")


def ensure_can_set_active(actor: Actor, target: Actor) -> None:
    ensure_can_modify_user(actor, target)
    if actor.id == target.id:
        raise PermissionDenied("Нельзя заблокировать собственную учётную запись", code="cannot_block_self")


# ------------------------------------------------------------------ документы и анализ


def can_upload_documents(actor: Actor) -> bool:
    return actor.role in CONTRIBUTOR_ROLES


def document_capabilities(
    actor: Actor, owner_id: object, granted: DocumentPermission | None
) -> dict[str, bool]:
    """Флаги для UI. Те же правила, что в SQL-условиях `app.security.scoped` (они и есть защита)."""
    contributor = actor.role in CONTRIBUTOR_ROLES
    is_owner = owner_id == actor.id
    return {
        "can_manage": is_superadmin(actor) or (contributor and is_owner),
        "can_share": contributor and is_owner,
        "can_analyze": is_superadmin(actor)
        or (contributor and (is_owner or granted is DocumentPermission.analyze)),
    }


def can_view_audit_log(actor: Actor) -> bool:
    return is_admin(actor)


# ------------------------------------------------------------------ провайдеры и модели


def can_manage_models(actor: Actor) -> bool:
    """Модели: добавление, включение, роли, проверка, импорт (Superadmin и Admin)."""
    return is_admin(actor)


def can_manage_providers(actor: Actor) -> bool:
    """Провайдеры: адрес, ключ, частная сеть, включение — только Superadmin (THREAT_MODEL §4.5)."""
    return is_superadmin(actor)


def can_manage_llm_settings(actor: Actor) -> bool:
    """Глобальный выключатель облачных моделей и текст предупреждения — только Superadmin."""
    return is_superadmin(actor)


def ensure(allowed: bool) -> None:
    if not allowed:
        raise PermissionDenied()
