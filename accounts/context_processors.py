from accounts.roles import is_admin, is_staff_member, role_display, role_home_url_name


def role_context(request):
    """Role flags + panel routing for templates — kept as cheap booleans
    computed from `request.user` (already loaded by AuthenticationMiddleware),
    no extra query. Navigation templates use these to render only the
    links a role is allowed to use; every one of those links is *also*
    enforced server-side by the view it points to (see
    accounts.decorators) — this context is presentation-only."""
    user = getattr(request, "user", None)
    return {
        "is_admin_role": is_admin(user),
        "is_staff_role": is_staff_member(user),
        "role_home_url_name": role_home_url_name(user) if user and user.is_authenticated else "login",
    }
