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
from django.utils import timezone

# url names reachable by an authenticated user who must change their
# password before anything else, or that must never trigger the
# deactivated-account bounce (logout itself, static assets).
_PASSWORD_CHANGE_EXEMPT = {"force_password_change", "logout"}

_IDLE_TIMEOUT_SECONDS = 30 * 60
_SESSION_LAST_ACTIVITY_KEY = "bazaar_last_activity_at"


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


class SessionSecurityMiddleware:
    """End a signed-in browser session after 30 minutes without activity.

    ``SESSION_EXPIRE_AT_BROWSER_CLOSE`` handles browser-close sign-out;
    this rolling server-side check handles an open but unattended tab.
    Static/media requests never count as activity, so page assets cannot
    silently keep a session alive.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith("/static/") or request.path.startswith("/media/"):
            return self.get_response(request)

        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            now = timezone.now().timestamp()
            previous = request.session.get(_SESSION_LAST_ACTIVITY_KEY)
            if previous is not None and now - float(previous) >= _IDLE_TIMEOUT_SECONDS:
                logout(request)
                messages.info(request, "You were logged out after 30 minutes of inactivity.")
                return redirect("login")
            request.session[_SESSION_LAST_ACTIVITY_KEY] = now

        return self.get_response(request)


class NoStoreForAuthenticatedMiddleware:
    """Marks every response served to a signed-in user as uncacheable.

    Without this, a browser can restore a dashboard/panel page straight
    from its back-forward cache after logout — the page still renders
    with the old session's data because no request ever reaches the
    server to notice the user is gone. `Cache-Control: no-store` opts
    the page out of bfcache (and disk/memory cache) entirely, so
    clicking Back after signing out forces a real request, which then
    hits `login_required`/`AccountStateMiddleware` like any other
    anonymous hit.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith("/static/") or request.path.startswith("/media/"):
            return response
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            response["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
            response["Pragma"] = "no-cache"
        return response
