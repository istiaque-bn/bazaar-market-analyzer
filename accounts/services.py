"""Account-management operations: creation, activation, role changes,
and password resets — every sensitive mutation here runs inside a
transaction and writes an AdminAuditLog row (market.services.audit).
Views call these instead of touching User/UserProfile directly, so the
audit trail and the final-active-admin guard can't be bypassed by a new
call site forgetting to add them.
"""
from __future__ import annotations

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils.crypto import get_random_string

from accounts.models import UserProfile
from accounts.roles import is_admin, is_staff_member
from market.models import AdminAuditAction
from market.services.audit import record_admin_action

# Unambiguous charset for a temp password shown once on screen — no
# 0/O/1/l/I, so a support call reading it aloud doesn't hit an
# ambiguous character.
_TEMP_PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789!@#$%"


class AccountActionError(Exception):
    """Raised for a rule violation the caller should show to the user
    (final-admin protection, wrong role for the action, ...) rather than
    a 500."""


def generate_temp_password(length: int = 16) -> str:
    return get_random_string(length, allowed_chars=_TEMP_PASSWORD_ALPHABET)


def active_admin_count() -> int:
    return User.objects.filter(is_superuser=True, is_staff=True, is_active=True).count()


@transaction.atomic
def create_account(actor, *, username, first_name, last_name, email, role: str, is_active: bool, request):
    """`role` is "user" or "staff" — never "admin"; Admin accounts are
    only ever created via `manage.py createsuperuser` (see docs), so
    there is no code path here that can mint one."""
    if role not in ("user", "staff"):
        raise AccountActionError("Invalid role.")
    if role == "staff" and not is_admin(actor):
        raise PermissionDenied("Only Admin accounts can create Staff accounts.")

    temp_password = generate_temp_password()
    user = User.objects.create_user(
        username=username,
        email=email,
        password=temp_password,
        first_name=first_name,
        last_name=last_name,
        is_active=is_active,
        is_staff=(role == "staff"),
        is_superuser=False,
    )
    # The post_save signal (accounts.signals.create_user_defaults)
    # already created the profile — fetch, don't re-create.
    profile = user.profile
    profile.created_by = actor if getattr(actor, "is_authenticated", False) else None
    profile.must_change_password = True
    profile.save(update_fields=["created_by", "must_change_password"])

    record_admin_action(
        request,
        AdminAuditAction.ACCOUNT_CREATED,
        {"role": role, "is_active": is_active},
        target_user=user,
    )
    record_admin_action(
        request,
        AdminAuditAction.TEMP_PASSWORD_ASSIGNED,
        {"reason": "account_created"},
        target_user=user,
    )
    return user, temp_password


@transaction.atomic
def set_active(actor, target: User, active: bool, request):
    if not (is_admin(actor) or is_staff_member(actor)):
        raise PermissionDenied("Only Admin and Staff accounts can activate/deactivate accounts.")
    if is_admin(target) and not is_admin(actor):
        raise PermissionDenied("Only an Admin can change another Admin's account.")
    if is_staff_member(actor) and target.is_staff:
        raise PermissionDenied("Staff can only activate/deactivate regular User accounts.")
    if not active and is_admin(target) and target.is_active and active_admin_count() <= 1:
        raise AccountActionError("This is the last active Admin account — it can't be deactivated.")

    if target.is_active == active:
        return target  # no-op, nothing to audit
    target.is_active = active
    target.save(update_fields=["is_active"])
    record_admin_action(
        request,
        AdminAuditAction.ACCOUNT_ACTIVATED if active else AdminAuditAction.ACCOUNT_DEACTIVATED,
        {},
        target_user=target,
    )
    return target


@transaction.atomic
def promote_to_staff(actor, target: User, request):
    if not is_admin(actor):
        raise PermissionDenied("Only an Admin can change roles.")
    if is_admin(target):
        raise AccountActionError("Admin accounts are not managed through role promotion.")
    if target.is_staff:
        raise AccountActionError(f"{target.username} is already Staff.")
    target.is_staff = True
    target.save(update_fields=["is_staff"])
    record_admin_action(request, AdminAuditAction.ROLE_PROMOTED, {"to": "staff"}, target_user=target)
    return target


@transaction.atomic
def demote_to_user(actor, target: User, request):
    if not is_admin(actor):
        raise PermissionDenied("Only an Admin can change roles.")
    if is_admin(target):
        raise AccountActionError("Admin accounts are not managed through role demotion.")
    if not target.is_staff:
        raise AccountActionError(f"{target.username} is already a regular User.")
    target.is_staff = False
    target.save(update_fields=["is_staff"])
    record_admin_action(request, AdminAuditAction.ROLE_DEMOTED, {"to": "user"}, target_user=target)
    return target


@transaction.atomic
def reset_password(actor, target: User, request) -> str:
    """Admin can reset a Staff or User's password; Staff can only reset a
    regular User's password (mirrors set_active's role guard)."""
    if not (is_admin(actor) or is_staff_member(actor)):
        raise PermissionDenied("Only Admin and Staff accounts can reset another account's password.")
    if is_admin(target) and not is_admin(actor):
        raise PermissionDenied("Only an Admin can reset another Admin's password.")
    if is_staff_member(actor) and target.is_staff:
        raise PermissionDenied("Staff can only reset a regular User's password.")

    temp_password = generate_temp_password()
    target.set_password(temp_password)
    target.save(update_fields=["password"])
    profile, _ = UserProfile.objects.get_or_create(user=target)
    profile.must_change_password = True
    profile.save(update_fields=["must_change_password"])
    record_admin_action(request, AdminAuditAction.PASSWORD_RESET_INITIATED, {}, target_user=target)
    return temp_password
