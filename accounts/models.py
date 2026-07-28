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

    def __str__(self):
        return f"Profile({self.user.username})"
