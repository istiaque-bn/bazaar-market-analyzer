from django.conf import settings
from django.db import models


class UserProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    telegram_chat_id = models.CharField(max_length=64, blank=True)
    email_alerts = models.BooleanField(default=True)
    telegram_alerts = models.BooleanField(default=False)
    min_score_alert = models.FloatField(default=40)
    preferred_exchanges = models.CharField(max_length=16, default="DSE,CSE")
    created_at = models.DateTimeField(auto_now_add=True)

    # --- Role-management system -------------------------------------
    # Who created this account (Admin/Staff, via the account-management
    # panel) — blank/null for accounts that predate this feature or were
    # created directly with `createsuperuser`/`manage.py shell`. SET_NULL
    # so the audit value survives the creator's account being deleted.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="accounts_created",
    )
    # Set when an Admin/Staff assigns a temporary password; forces the
    # holder through a password-change screen (see
    # accounts.middleware.AccountStateMiddleware) before they can reach
    # anything else. Never set for self-service password changes.
    must_change_password = models.BooleanField(default=False)

    def __str__(self):
        return f"Profile({self.user.username})"
