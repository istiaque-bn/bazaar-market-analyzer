"""View-level role gates.

Response policy (see README's "Roles & permissions" section):
  anonymous            -> redirect to Login (with `next`)
  authenticated, wrong role -> 403 Forbidden (PermissionDenied)

These are deliberately view/decorator-level, not just "hide the link" —
every sensitive queryset/service call these decorators guard also
re-checks ownership/role itself (see accounts.views and
market.services.portfolio), so a decorator bypass alone can't leak data.
"""
from __future__ import annotations

from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied

from accounts.roles import is_admin, is_staff_member


def _role_required(test_func, message):
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if not test_func(request.user):
                raise PermissionDenied(message)
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator


admin_required = _role_required(is_admin, "This page is restricted to Admin accounts.")
staff_or_admin_required = _role_required(
    lambda u: is_admin(u) or is_staff_member(u), "This page is restricted to Staff and Admin accounts."
)
