from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.paginator import Paginator
from django.db.models import Q
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from accounts.decorators import admin_required, staff_or_admin_required
from accounts.forms import (
    AdminCreateAccountForm,
    BazaarAuthenticationForm,
    ForcedPasswordChangeForm,
    ProfileForm,
    StaffCreateAccountForm,
)
from accounts.models import UserProfile
from accounts.roles import is_admin, role_display, role_home_url
from accounts.services import (
    AccountActionError,
    create_account,
    demote_to_user,
    promote_to_staff,
    reset_password,
    set_active,
)
from market.models import AdminAuditAction, AdminAuditLog
from market.services.audit import record_admin_action

User = get_user_model()

ACCOUNT_EVENT_ACTIONS = [
    AdminAuditAction.ACCOUNT_CREATED,
    AdminAuditAction.ACCOUNT_ACTIVATED,
    AdminAuditAction.ACCOUNT_DEACTIVATED,
    AdminAuditAction.ACCOUNT_DELETED,
    AdminAuditAction.ROLE_PROMOTED,
    AdminAuditAction.ROLE_DEMOTED,
    AdminAuditAction.PASSWORD_RESET_INITIATED,
    AdminAuditAction.TEMP_PASSWORD_ASSIGNED,
    AdminAuditAction.FIRST_LOGIN_PASSWORD_CHANGED,
]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class BazaarLoginView(LoginView):
    """Redirects an already-authenticated visitor straight to the Market
    dashboard (redirect_authenticated_user), and — absent a safe `next` —
    sends a freshly-logged-in user there too, instead of a single fixed
    LOGIN_REDIRECT_URL. `next` itself is still validated by the base
    LoginView/SuccessURLAllowedHostsMixin machinery
    (url_has_allowed_host_and_scheme), which already rejects external and
    protocol-relative targets — nothing here weakens that check."""

    template_name = "registration/login.html"
    authentication_form = BazaarAuthenticationForm
    redirect_authenticated_user = True

    def get_default_redirect_url(self):
        if self.request.user.is_authenticated:
            return role_home_url(self.request.user)
        return super().get_default_redirect_url()

    def form_valid(self, form):
        """Blocks sign-in with a temp password past its 15-minute
        deadline (accounts.services.TEMP_PASSWORD_TTL) — checked here,
        before the base LoginView calls auth_login(), so an expired temp
        password never even gets a session. A normal (non-temp) password
        has no expiry and is unaffected."""
        user = form.get_user()
        profile = getattr(user, "profile", None)
        expires_at = getattr(profile, "temp_password_expires_at", None)
        if profile is not None and profile.must_change_password and expires_at and timezone.now() > expires_at:
            form.add_error(None, "Your temporary password has expired. Contact an administrator for a new one.")
            return self.form_invalid(form)
        return super().form_valid(form)


def signup_disabled(request):
    """Tombstone for the old public-registration URL. GET and POST both
    refuse and nothing is ever created — kept resolvable (rather than
    deleted from urls.py) so an old bookmark/link gets a clear
    explanation instead of a bare, unexplained 404."""
    return render(request, "registration/signup_disabled.html", status=403)


@login_required
@require_http_methods(["GET", "POST"])
def force_password_change(request):
    """Reached automatically by accounts.middleware.AccountStateMiddleware
    whenever profile.must_change_password is set (temp password just
    assigned by an Admin/Staff). Doesn't ask for the temp password again
    — logging in already proved possession of it."""
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    if not profile.must_change_password:
        return redirect(role_home_url(request.user))
    if request.method == "POST":
        form = ForcedPasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            request.user.set_password(form.cleaned_data["new_password2"])
            request.user.save(update_fields=["password"])
            profile.must_change_password = False
            profile.temp_password_expires_at = None
            profile.save(update_fields=["must_change_password", "temp_password_expires_at"])
            update_session_auth_hash(request, request.user)
            record_admin_action(
                request, AdminAuditAction.FIRST_LOGIN_PASSWORD_CHANGED, {}, target_user=request.user
            )
            messages.success(request, "Password updated.")
            return redirect(role_home_url(request.user))
    else:
        form = ForcedPasswordChangeForm(request.user)
    return render(request, "accounts/force_password_change.html", {"form": form})


# ---------------------------------------------------------------------------
# Profile (unchanged behaviour, pre-existing)
# ---------------------------------------------------------------------------


@login_required
@require_http_methods(["GET", "POST"])
def profile(request):
    profile_obj, _ = UserProfile.objects.get_or_create(user=request.user)
    if request.method == "POST":
        form = ProfileForm(request.POST)
        if not form.is_valid():
            for field_errors in form.errors.values():
                for error in field_errors:
                    messages.error(request, error)
            return render(request, "accounts/profile.html", {"profile": profile_obj})
        data = form.cleaned_data
        profile_obj.telegram_chat_id = data["telegram_chat_id"]
        profile_obj.email_alerts = data["email_alerts"]
        profile_obj.telegram_alerts = data["telegram_alerts"]
        profile_obj.min_score_alert = data["min_score_alert"]
        profile_obj.preferred_exchanges = data["preferred_exchanges"]
        profile_obj.save()
        if "email" in request.POST:
            request.user.email = data["email"]
            request.user.save(update_fields=["email"])
        return redirect("profile")
    return render(request, "accounts/profile.html", {"profile": profile_obj})


# ---------------------------------------------------------------------------
# Role panels
# ---------------------------------------------------------------------------


@admin_required
def admin_panel(request):
    from market.services.market_hours import both_exchanges_status
    from market.services.ops_alerts import evaluate_alerts
    from market.services.ops_metrics import ops_summary

    counts = {
        "total": User.objects.count(),
        "admin": User.objects.filter(is_superuser=True, is_staff=True).count(),
        "staff": User.objects.filter(is_staff=True, is_superuser=False).count(),
        "user": User.objects.filter(is_staff=False, is_superuser=False).count(),
        "active": User.objects.filter(is_active=True).count(),
        "inactive": User.objects.filter(is_active=False).count(),
    }
    recent_accounts = User.objects.order_by("-date_joined")[:8]
    recent_events = (
        AdminAuditLog.objects.filter(action__in=ACCOUNT_EVENT_ACTIONS)
        .select_related("user", "target_user")
        .order_by("-created_at")[:8]
    )

    summary = None
    alerts = []
    try:
        summary = ops_summary()
        alerts = evaluate_alerts(summary)
    except Exception:
        summary = None

    ml_groups = []
    try:
        from market.services.reliability_report import latest_assessments

        ml_groups = list(latest_assessments())
    except Exception:
        ml_groups = []

    from market.models import TaskRun

    recent_tasks = TaskRun.objects.order_by("-started_at")[:6]

    automation = None
    try:
        from market.services.automation_status import automation_status_snapshot

        automation = automation_status_snapshot(summary=summary, alerts=alerts)
    except Exception:
        automation = None

    feedback_summary = None
    try:
        from feedback.services import admin_dashboard_summary

        feedback_summary = admin_dashboard_summary()
    except Exception:
        feedback_summary = None

    telegram_report = None
    try:
        from market.services.automation_status import telegram_report_status

        telegram_report = telegram_report_status()
    except Exception:
        telegram_report = None

    return render(
        request,
        "accounts/panel_admin.html",
        {
            "counts": counts,
            "recent_accounts": recent_accounts,
            "recent_events": recent_events,
            "market_hours": both_exchanges_status(),
            "ops_summary": summary,
            "ops_alerts": alerts,
            # The panel links to the dedicated data-quality report. Do not
            # build that report here too: it can be expensive and was only
            # used to decide whether to show this always-useful link.
            "quality": True,
            "ml_groups": ml_groups,
            "recent_tasks": recent_tasks,
            "automation": automation,
            "feedback_summary": feedback_summary,
            "telegram_report": telegram_report,
        },
    )


@staff_or_admin_required
def staff_panel(request):
    from market.services.market_hours import both_exchanges_status
    from market.services.ops_alerts import evaluate_alerts
    from market.services.ops_metrics import ops_summary

    user_count = User.objects.filter(is_staff=False, is_superuser=False).count()
    recent_users = User.objects.filter(is_staff=False, is_superuser=False).order_by("-date_joined")[:8]

    alerts = []
    try:
        alerts = evaluate_alerts(ops_summary())
    except Exception:
        alerts = []

    from market.models import TaskRun

    recent_tasks = TaskRun.objects.order_by("-started_at")[:6]

    return render(
        request,
        "accounts/panel_staff.html",
        {
            "user_count": user_count,
            "recent_users": recent_users,
            "market_hours": both_exchanges_status(),
            "ops_alerts": alerts,
            "recent_tasks": recent_tasks,
        },
    )


@login_required
def user_panel(request):
    from market.models import Watchlist
    from market.services.market_hours import both_exchanges_status
    from market.services.portfolio import get_or_create_default_portfolio, portfolio_summary
    from notifications.models import Alert

    portfolio = get_or_create_default_portfolio(request.user)
    summary = portfolio_summary(portfolio)
    watchlist, _ = Watchlist.objects.get_or_create(user=request.user, name="Default")
    watchlist_count = watchlist.stocks.count()
    recent_transactions = list(
        portfolio.transactions.select_related("stock").order_by("-transaction_date", "-created_at")[:5]
    )
    alerts = list(Alert.objects.filter(Q(user=request.user) | Q(user__isnull=True)).order_by("-created_at")[:5])

    return render(
        request,
        "accounts/panel_user.html",
        {
            "portfolio": portfolio,
            "summary": summary,
            "watchlist_count": watchlist_count,
            "recent_transactions": recent_transactions,
            "alerts": alerts,
            "market_hours": both_exchanges_status(),
        },
    )


# ---------------------------------------------------------------------------
# Account management (Admin + Staff, scope enforced per-action)
# ---------------------------------------------------------------------------


def _manageable_queryset(actor):
    """Staff only ever sees/acts on regular Users; Admin sees everyone.
    Applied at the queryset level (not just hidden in the template) so a
    Staff request for an Admin/Staff account_id 404s exactly like a
    nonexistent one."""
    qs = User.objects.select_related("profile").order_by("-date_joined")
    if is_admin(actor):
        return qs
    return qs.filter(is_staff=False, is_superuser=False)


@staff_or_admin_required
def account_list(request):
    qs = _manageable_queryset(request.user)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(username__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(email__icontains=q)
        )
    role = request.GET.get("role", "").strip().lower()
    if role == "admin" and is_admin(request.user):
        qs = qs.filter(is_superuser=True, is_staff=True)
    elif role == "staff" and is_admin(request.user):
        qs = qs.filter(is_staff=True, is_superuser=False)
    elif role == "user":
        qs = qs.filter(is_staff=False, is_superuser=False)
    status = request.GET.get("status", "").strip().lower()
    if status == "active":
        qs = qs.filter(is_active=True)
    elif status == "inactive":
        qs = qs.filter(is_active=False)
    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    rows = [(u, role_display(u)) for u in page]
    return render(
        request,
        "accounts/account_list.html",
        {"page_obj": page, "rows": rows, "q": q, "role": role, "status": status},
    )


@staff_or_admin_required
def account_create(request, target_role: str):
    """`target_role` ("user" or "staff") comes from urls.py — fixed per
    URL, never from client-submitted data. "Create Staff" is only
    reachable via a URL an Admin-only nav link points to, and is
    re-checked server-side below regardless of how the request arrived.
    Neither form class exposes a `role` field, so there is no field
    whose value could override this."""
    if target_role not in ("user", "staff"):
        from django.http import Http404

        raise Http404

    if target_role == "staff" and not is_admin(request.user):
        from django.core.exceptions import PermissionDenied

        raise PermissionDenied("Only Admin accounts can create Staff accounts.")

    form_cls = AdminCreateAccountForm if is_admin(request.user) else StaffCreateAccountForm

    if request.method == "POST":
        form = form_cls(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            try:
                user, temp_password, telegram_sent, email_sent = create_account(
                    request.user,
                    username=data["username"],
                    first_name=data["first_name"],
                    last_name=data["last_name"],
                    email=data["email"],
                    telegram_chat_id=data["telegram_chat_id"],
                    role=target_role,
                    is_active=data["is_active"],
                    request=request,
                )
            except Exception as exc:
                messages.error(request, str(exc))
            else:
                if not telegram_sent and not email_sent:
                    messages.warning(
                        request, "Couldn't deliver the temporary password via Telegram or email — share it from below instead."
                    )
                elif not telegram_sent:
                    messages.info(request, "Telegram delivery failed, but the temporary password was sent by email.")
                return render(
                    request,
                    "accounts/account_created.html",
                    {
                        "created_user": user,
                        "temp_password": temp_password,
                        "role": role_display(user),
                        "telegram_sent": telegram_sent,
                        "email_sent": email_sent,
                    },
                )
    else:
        form = form_cls()
    return render(
        request,
        "accounts/account_form.html",
        {"form": form, "target_role": target_role, "title": f"Create {target_role.title()} account"},
    )


def _get_manageable_or_404(request, user_id):
    return get_object_or_404(_manageable_queryset(request.user), id=user_id)


@staff_or_admin_required
def account_detail(request, user_id):
    target = _get_manageable_or_404(request, user_id)
    events = (
        AdminAuditLog.objects.filter(target_user=target, action__in=ACCOUNT_EVENT_ACTIONS)
        .select_related("user")
        .order_by("-created_at")[:20]
    )
    return render(
        request,
        "accounts/account_detail.html",
        {
            "target": target,
            "target_role": role_display(target),
            "events": events,
            "can_change_role": is_admin(request.user) and not is_admin(target),
            "is_last_active_admin": is_admin(target) and target.is_active and User.objects.filter(
                is_superuser=True, is_staff=True, is_active=True
            ).count() <= 1,
        },
    )


@staff_or_admin_required
@require_POST
def account_activate(request, user_id):
    target = _get_manageable_or_404(request, user_id)
    try:
        set_active(request.user, target, True, request)
        messages.success(request, f"{target.username}'s account is now active.")
    except Exception as exc:
        messages.error(request, str(exc))
    return redirect("account_detail", user_id=target.id)


@staff_or_admin_required
@require_POST
def account_deactivate(request, user_id):
    target = _get_manageable_or_404(request, user_id)
    if request.POST.get("confirm_username") != target.username:
        messages.error(request, "Username didn't match — nothing was changed.")
        return redirect("account_detail", user_id=target.id)
    try:
        set_active(request.user, target, False, request)
        messages.success(request, f"{target.username}'s account has been deactivated.")
    except Exception as exc:
        messages.error(request, str(exc))
    return redirect("account_detail", user_id=target.id)


@admin_required
@require_POST
def account_delete(request, user_id):
    """Permanently delete a regular/Staff account after typed confirmation.
    Admin accounts are deliberately never deletable here; audit rows retain
    the target username snapshot after the account is removed."""
    target = get_object_or_404(User, id=user_id)
    if target == request.user or is_admin(target):
        messages.error(request, "Admin accounts cannot be deleted from account management.")
        return redirect("account_detail", user_id=target.id)
    if request.POST.get("confirm_username") != target.username:
        messages.error(request, "Username didn't match — nothing was deleted.")
        return redirect("account_detail", user_id=target.id)
    username = target.username
    with transaction.atomic():
        record_admin_action(request, AdminAuditAction.ACCOUNT_DELETED, {"role": role_display(target)}, target_user=target)
        target.delete()
    messages.success(request, f"{username}'s account was permanently deleted.")
    return redirect("account_list")


@admin_required
@require_POST
def account_promote(request, user_id):
    target = get_object_or_404(User, id=user_id)
    if request.POST.get("confirm_username") != target.username:
        messages.error(request, "Username didn't match — nothing was changed.")
        return redirect("account_detail", user_id=target.id)
    try:
        promote_to_staff(request.user, target, request)
        messages.success(request, f"{target.username} promoted to Staff.")
    except Exception as exc:
        messages.error(request, str(exc))
    return redirect("account_detail", user_id=target.id)


@admin_required
@require_POST
def account_demote(request, user_id):
    target = get_object_or_404(User, id=user_id)
    if request.POST.get("confirm_username") != target.username:
        messages.error(request, "Username didn't match — nothing was changed.")
        return redirect("account_detail", user_id=target.id)
    try:
        demote_to_user(request.user, target, request)
        messages.success(request, f"{target.username} demoted to User.")
    except Exception as exc:
        messages.error(request, str(exc))
    return redirect("account_detail", user_id=target.id)


@staff_or_admin_required
@require_POST
def account_reset_password(request, user_id):
    target = _get_manageable_or_404(request, user_id)
    try:
        temp_password, telegram_sent = reset_password(request.user, target, request)
    except Exception as exc:
        messages.error(request, str(exc))
        return redirect("account_detail", user_id=target.id)
    if not telegram_sent:
        messages.warning(
            request, "Couldn't deliver the temporary password via Telegram — share it from below instead."
        )
    return render(
        request,
        "accounts/account_password_reset.html",
        {"target": target, "temp_password": temp_password, "telegram_sent": telegram_sent},
    )
