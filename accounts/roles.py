"""Canonical role source for Bazaar.

Roles are derived purely from Django's own `is_staff`/`is_superuser`
flags — there is no separate `role` field that could drift out of sync
with them:

    Admin: is_superuser=True and is_staff=True
    Staff: is_staff=True  and is_superuser=False
    User:  is_staff=False and is_superuser=False

An inactive user has no role for access purposes (every helper below
returns False/None for `is_active=False`), even though their historical
`is_staff`/`is_superuser` flags are left untouched.
"""
from __future__ import annotations

from django.urls import reverse

ADMIN = "admin"
STAFF = "staff"
USER = "user"


def _active(user) -> bool:
    return bool(user and getattr(user, "is_authenticated", False) and user.is_active)


def is_admin(user) -> bool:
    return _active(user) and user.is_superuser and user.is_staff


def is_staff_member(user) -> bool:
    return _active(user) and user.is_staff and not user.is_superuser


def is_regular_user(user) -> bool:
    return _active(user) and not user.is_staff and not user.is_superuser


def role_name(user) -> str | None:
    if is_admin(user):
        return ADMIN
    if is_staff_member(user):
        return STAFF
    if is_regular_user(user):
        return USER
    return None


def role_display(user) -> str:
    return {ADMIN: "Admin", STAFF: "Staff", USER: "User"}.get(role_name(user), "—")


def role_name_for(target_user) -> str:
    """Same classification as `role_name`, but for a user object being
    displayed/managed rather than the requesting `request.user` — doesn't
    require `is_authenticated`/`is_active` (an inactive account still has
    a role label in an accounts list, it just can't act)."""
    if target_user.is_superuser and target_user.is_staff:
        return ADMIN
    if target_user.is_staff:
        return STAFF
    return USER


def role_home_url_name(user) -> str:
    return {ADMIN: "admin_panel", STAFF: "staff_panel", USER: "user_panel"}.get(role_name(user), "login")


def role_home_url(user) -> str:
    return reverse(role_home_url_name(user))
