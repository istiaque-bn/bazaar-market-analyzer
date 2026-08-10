from django.conf import settings
from django.db import models

from market.models import AnalysisResult, Stock


def mask_recipient(chat_id: str) -> str:
    """Safely-displayable form of a Telegram chat id — never the raw
    value. Keeps the first/last 2 characters (enough for an admin to
    recognize "yes, that's the configured one" without exposing it) and
    masks the rest."""
    chat_id = (chat_id or "").strip()
    if len(chat_id) <= 4:
        return "*" * len(chat_id)
    return f"{chat_id[:2]}{'*' * (len(chat_id) - 4)}{chat_id[-2:]}"


class AlertChannel(models.TextChoices):
    TELEGRAM = "telegram", "Telegram"
    EMAIL = "email", "Email"
    IN_APP = "in_app", "In-app"


class Alert(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="alerts",
    )
    stock = models.ForeignKey(Stock, on_delete=models.CASCADE, null=True, blank=True, related_name="alerts")
    analysis = models.ForeignKey(
        AnalysisResult, on_delete=models.SET_NULL, null=True, blank=True, related_name="alerts"
    )
    channel = models.CharField(max_length=16, choices=AlertChannel.choices, default=AlertChannel.IN_APP)
    title = models.CharField(max_length=200)
    message = models.TextField()
    is_sent = models.BooleanField(default=False)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class AdminReminder(models.Model):
    admin = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="admin_reminders")
    remind_on = models.DateField(db_index=True)
    action = models.TextField(max_length=1000)
    telegram_enabled = models.BooleanField(default=True)
    email_enabled = models.BooleanField(default=False)
    delivered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["remind_on", "id"]


class MlDailyReportStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SENT = "sent", "Sent"
    RETRYING = "retrying", "Retrying"
    FAILED = "failed", "Failed"
    SKIPPED = "skipped", "Skipped"


class MlDailyReportDelivery(models.Model):
    """One row per (report_date, recipient) — the audit trail and
    duplicate-prevention record for the Telegram ML daily report. Never
    stores the bot token or the raw chat id (see notifications.models.
    mask_recipient); a retry re-uses the same row (matched by
    idempotency_key) rather than creating a new one, so a confirmed
    delivery can never be silently duplicated by a retried task."""

    report_date = models.DateField(db_index=True)
    recipient_masked = models.CharField(max_length=32)
    idempotency_key = models.CharField(max_length=64, unique=True)

    generated_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=MlDailyReportStatus.choices, default=MlDailyReportStatus.PENDING)

    telegram_message_id = models.CharField(max_length=32, blank=True)
    attempt_count = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True, help_text="Redacted — never contains the bot token.")

    content_hash = models.CharField(max_length=32, blank=True)
    model_version_summary = models.CharField(max_length=255, blank=True)
    report_text = models.TextField(blank=True, help_text="The generated report, for preview/audit — sent or not.")
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-report_date", "-generated_at"]

    def __str__(self):
        return f"{self.report_date} -> {self.recipient_masked} [{self.status}]"
