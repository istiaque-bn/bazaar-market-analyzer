"""Phase 9: record who did what for staff actions with real operational
consequence. See market.models.AdminAuditLog."""
from __future__ import annotations

from market.models import AdminAuditLog


def _client_ip(request) -> str | None:
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


def record_admin_action(request, action: str, detail: dict | None = None) -> AdminAuditLog:
    user = getattr(request, "user", None)
    user = user if (user is not None and getattr(user, "is_authenticated", False)) else None
    return AdminAuditLog.objects.create(
        user=user,
        username_snapshot=user.get_username() if user else "",
        action=action,
        detail=detail or {},
        ip_address=_client_ip(request),
    )
