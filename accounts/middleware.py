"""Session/account-state enforcement for the role-management system.

Django's `AuthenticationMiddleware` resolves `request.user` from the
session on every request but does **not** re-check `is_active` after
initial login — only `authenticate()` does that. Without this
middleware, an account deactivated mid-session would keep working until
the session naturally expired. This middleware closes that gap, and
also enforces the "must change password before doing anything else"
state left behind by an Admin/Staff-assigned temporary password.

Runs after AuthenticationMiddleware (needs `request.user`) and before
the view.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.urls import NoReverseMatch, resolve, reverse

# url names reachable by an authenticated user who must change their
# password before anything else, or that must never trigger the
# deactivated-account bounce (logout itself, static assets).
_PASSWORD_CHANGE_EXEMPT = {"force_password_change", "logout"}


class AccountStateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith("/static/") or request.path.startswith("/media/"):
            return self.get_response(request)

        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            if not user.is_active:
                logout(request)
                messages.error(request, "This account has been deactivated. Contact an administrator.")
                return redirect("login")

            profile = getattr(user, "profile", None)
            if profile is not None and profile.must_change_password:
                try:
                    url_name = resolve(request.path).url_name
                except Exception:
                    url_name = None
                if url_name not in _PASSWORD_CHANGE_EXEMPT:
                    try:
                        return redirect(reverse("force_password_change"))
                    except NoReverseMatch:
                        pass

        return self.get_response(request)
