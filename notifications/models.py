from django.conf import settings
from django.db import models

from market.models import AnalysisResult, Stock


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
