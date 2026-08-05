"""DRF role permissions mirroring accounts.roles / accounts.decorators —
same Admin = is_superuser+is_staff / Staff = is_staff only canonical
split, just expressed as `rest_framework.permissions.BasePermission`
subclasses instead of view decorators."""
from __future__ import annotations

from rest_framework.permissions import BasePermission

from accounts.roles import is_admin, is_staff_member


class IsBazaarAdmin(BasePermission):
    message = "This endpoint is restricted to Admin accounts."

    def has_permission(self, request, view):
        return is_admin(request.user)


class IsBazaarStaffOrAdmin(BasePermission):
    message = "This endpoint is restricted to Staff and Admin accounts."

    def has_permission(self, request, view):
        return is_admin(request.user) or is_staff_member(request.user)
