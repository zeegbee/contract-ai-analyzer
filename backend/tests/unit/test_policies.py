"""Политики RBAC без БД."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest

from app.errors import PermissionDenied
from app.models.enums import UserRole
from app.security import policies


@dataclass
class FakeUser:
    role: UserRole
    id: uuid.UUID = field(default_factory=uuid.uuid4)


SUPER, ADMIN, ANALYST, VIEWER = UserRole.superadmin, UserRole.admin, UserRole.analyst, UserRole.viewer


def test_assignable_roles() -> None:
    assert policies.assignable_roles(FakeUser(SUPER)) == frozenset(UserRole)
    assert policies.assignable_roles(FakeUser(ADMIN)) == {ADMIN, ANALYST, VIEWER}
    assert policies.assignable_roles(FakeUser(ANALYST)) == frozenset()
    assert policies.assignable_roles(FakeUser(VIEWER)) == frozenset()


@pytest.mark.parametrize("role", [ANALYST, VIEWER])
def test_non_admins_cannot_manage_users(role: UserRole) -> None:
    with pytest.raises(PermissionDenied):
        policies.ensure_can_manage_users(FakeUser(role))
    with pytest.raises(PermissionDenied):
        policies.ensure_can_modify_user(FakeUser(role), FakeUser(VIEWER))


def test_admin_cannot_assign_superadmin() -> None:
    with pytest.raises(PermissionDenied) as exc:
        policies.ensure_can_assign_role(FakeUser(ADMIN), FakeUser(ANALYST), SUPER)
    assert exc.value.code == "role_not_assignable"


def test_admin_cannot_touch_superadmin() -> None:
    target, admin = FakeUser(SUPER), FakeUser(ADMIN)
    with pytest.raises(PermissionDenied) as exc:
        policies.ensure_can_modify_user(admin, target)
    assert exc.value.code == "superadmin_protected"
    with pytest.raises(PermissionDenied) as exc:
        policies.ensure_can_assign_role(admin, target, VIEWER)
    assert exc.value.code == "superadmin_protected"
    with pytest.raises(PermissionDenied) as exc:
        policies.ensure_can_set_active(admin, target)
    assert exc.value.code == "superadmin_protected"


def test_admin_can_manage_other_admins_and_below() -> None:
    actor = FakeUser(ADMIN)
    for role in (ADMIN, ANALYST, VIEWER):
        policies.ensure_can_assign_role(actor, FakeUser(role), VIEWER)
        policies.ensure_can_set_active(actor, FakeUser(role))


@pytest.mark.parametrize("role", [SUPER, ADMIN])
def test_nobody_changes_own_role_or_blocks_self(role: UserRole) -> None:
    me = FakeUser(role)
    with pytest.raises(PermissionDenied) as exc:
        policies.ensure_can_assign_role(me, me, VIEWER)
    assert exc.value.code == "cannot_change_own_role"
    with pytest.raises(PermissionDenied) as exc:
        policies.ensure_can_set_active(me, me)
    assert exc.value.code == "cannot_block_self"


def test_superadmin_can_assign_superadmin() -> None:
    policies.ensure_can_assign_role(FakeUser(SUPER), FakeUser(ADMIN), SUPER)


def test_upload_and_audit_capabilities() -> None:
    assert [policies.can_upload_documents(FakeUser(r)) for r in (SUPER, ADMIN, ANALYST, VIEWER)] == [
        True,
        True,
        True,
        False,
    ]
    assert [policies.can_view_audit_log(FakeUser(r)) for r in (SUPER, ADMIN, ANALYST, VIEWER)] == [
        True,
        True,
        False,
        False,
    ]
