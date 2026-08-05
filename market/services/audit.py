"""Phase 9 (+ role-management system): record who did what for staff
actions with real operational consequence, and for sensitive
account-management events (creation, activation, role changes, password
resets). See market.models.AdminAuditLog.

`detail` must never carry a plaintext password, password hash, complete
reset token, session cookie, authorization header, or secret environment
value — callers pass only safe, descriptive fields (e.g. "role":
"staff", not the credential itself)."""
from __future__ import annotations

from market.models import AdminAuditLog


def _client_ip(request) -> str | None:
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


def record_admin_action(request, action: str, detail: dict | None = None, target_user=None) -> AdminAuditLog:
    user = getattr(request, "user", None)
    user = user if (user is not None and getattr(user, "is_authenticated", False)) else None
    return AdminAuditLog.objects.create(
        user=user,
        username_snapshot=user.get_username() if user else "",
        target_user=target_user,
        target_username_snapshot=target_user.get_username() if target_user else "",
        action=action,
        detail=detail or {},
        ip_address=_client_ip(request),
        request_id=getattr(request, "request_id", "") or "",
    )
