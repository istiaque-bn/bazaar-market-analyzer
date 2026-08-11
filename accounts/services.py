"""Account-management operations: creation, activation, role changes,
and password resets — every sensitive mutation here runs inside a
transaction and writes an AdminAuditLog row (market.services.audit).
Views call these instead of touching User/UserProfile directly, so the
audit trail and the final-active-admin guard can't be bypassed by a new
call site forgetting to add them.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import get_random_string

from accounts.models import UserProfile
from accounts.roles import is_admin, is_staff_member
from market.models import AdminAuditAction
from market.services.audit import record_admin_action
from notifications.services import send_telegram_message

logger = logging.getLogger(__name__)

# Unambiguous charset for a temp password shown once on screen — no
# 0/O/1/l/I, so a support call reading it aloud doesn't hit an
# ambiguous character.
_TEMP_PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789!@#$%"

# How long a freshly assigned temp password is a valid login credential
# for — see accounts.views.BazaarLoginView.form_valid, which is what
# actually enforces this deadline.
TEMP_PASSWORD_TTL = timedelta(minutes=15)


class AccountActionError(Exception):
    """Raised for a rule violation the caller should show to the user
    (final-admin protection, wrong role for the action, ...) rather than
    a 500."""


def generate_temp_password(length: int = 16) -> str:
    return get_random_string(length, allowed_chars=_TEMP_PASSWORD_ALPHABET)


def active_admin_count() -> int:
    return User.objects.filter(is_superuser=True, is_staff=True, is_active=True).count()


def _deliver_temp_password_telegram(*, request, chat_id: str, username: str, temp_password: str, reason: str) -> bool:
    """Fire-and-forget delivery of a freshly (re)generated temp password —
    the one and only place these credentials leave the server. Runs the
    same Telegram bot as every other notification in this project (no
    second bot/token), and is always called via transaction.on_commit so
    nothing is ever sent for a User row that then gets rolled back."""
    login_url = request.build_absolute_uri(reverse("login")) if request is not None else "/accounts/login/"
    verb = "created" if reason == "account_created" else "reset"
    text = (
        f"\U0001f511 Your Bazaar account was just {verb}.\n"
        f"Username: {username}\n"
        f"Temporary password: {temp_password}\n\n"
        f"This password is only valid for {int(TEMP_PASSWORD_TTL.total_seconds() // 60)} minutes — "
        f"sign in now and set a new password before then:\n{login_url}"
    )
    sent = send_telegram_message(chat_id, text, token=settings.TELEGRAM_SECURITY_BOT_TOKEN)
    if not sent:
        logger.warning("Temp password Telegram delivery failed for %s (%s)", username, reason)
    return sent


def _deliver_temp_password_email(*, request, email: str, username: str, temp_password: str, reason: str) -> bool:
    """Best-effort SMTP delivery for a newly-created account's password.

    SMTP is deliberately opt-in: a development installation uses Django's
    console backend and production only sends when EMAIL_HOST is configured.
    Do not log the recipient address or the temporary credential.
    """
    if not email or not getattr(settings, "EMAIL_HOST", ""):
        return False

    login_url = request.build_absolute_uri(reverse("login")) if request is not None else "/accounts/login/"
    verb = "created" if reason == "account_created" else "reset"
    minutes = int(TEMP_PASSWORD_TTL.total_seconds() // 60)
    message = (
        f"Your Bazaar account was just {verb}.\n\n"
        f"Username: {username}\n"
        f"Temporary password: {temp_password}\n\n"
        f"This password is valid for {minutes} minutes. Sign in and change it before it expires:\n{login_url}"
    )
    try:
        sent = send_mail(
            subject="Your Bazaar temporary password",
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            fail_silently=False,
        )
    except Exception:
        logger.warning("Temp password email delivery failed for %s (%s)", username, reason, exc_info=True)
        return False
    if sent != 1:
        logger.warning("Temp password email delivery was not accepted for %s (%s)", username, reason)
        return False
    return True


def _notify_admin_account_created(*, username: str, role: str) -> bool:
    """Best-effort operational notification; never include credentials."""
    chat_id = getattr(settings, "TELEGRAM_ADMIN_CHAT_ID", "")
    if not chat_id:
        return False
    return send_telegram_message(
        chat_id,
        f"👤 Bazaar account created\nUsername: {username}\nRole: {role.title()}\nTemporary password was delivered separately.",
    )


@transaction.atomic
def create_account(
    actor, *, username, first_name, last_name, email, telegram_chat_id: str, role: str, is_active: bool, request
):
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
    profile.telegram_chat_id = telegram_chat_id
    profile.must_change_password = True
    profile.temp_password_expires_at = timezone.now() + TEMP_PASSWORD_TTL
    profile.save(
        update_fields=["created_by", "telegram_chat_id", "must_change_password", "temp_password_expires_at"]
    )

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
    telegram_sent = _deliver_temp_password_telegram(
        request=request,
        chat_id=telegram_chat_id,
        username=username,
        temp_password=temp_password,
        reason="account_created",
    )
    email_sent = _deliver_temp_password_email(
        request=request,
        email=user.email,
        username=username,
        temp_password=temp_password,
        reason="account_created",
    )
    _notify_admin_account_created(username=username, role=role)
    return user, temp_password, telegram_sent, email_sent


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
def reset_password(actor, target: User, request) -> tuple[str, bool]:
    """Admin can reset a Staff or User's password; Staff can only reset a
    regular User's password (mirrors set_active's role guard). Returns
    (temp_password, telegram_sent) — telegram_sent is False (not an
    error) when the account has no telegram_chat_id on file yet, e.g.
    one created before this feature existed."""
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
    profile.temp_password_expires_at = timezone.now() + TEMP_PASSWORD_TTL
    profile.save(update_fields=["must_change_password", "temp_password_expires_at"])
    record_admin_action(request, AdminAuditAction.PASSWORD_RESET_INITIATED, {}, target_user=target)

    telegram_sent = False
    if profile.telegram_chat_id:
        telegram_sent = _deliver_temp_password_telegram(
            request=request,
            chat_id=profile.telegram_chat_id,
            username=target.username,
            temp_password=temp_password,
            reason="password_reset",
        )
    else:
        logger.warning("No telegram_chat_id on file for %s; temp password shown on screen only", target.username)
    return temp_password, telegram_sent
