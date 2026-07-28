from unittest import mock

from django.contrib.auth.models import User
from django.db.models import Q
from django.test import TestCase, override_settings

from accounts.models import UserProfile
from notifications.models import Alert
from notifications.tasks import send_daily_digest


@override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="")
class DailyDigestDedupTests(TestCase):
    """send_daily_digest used to create both a global Alert(user=None) and an
    identical per-user Alert(user=user); views filter on
    Q(user=request.user) | Q(user__isnull=True), so every active user saw
    the digest twice. Only the global row should exist after the task runs."""

    def setUp(self):
        self.user = User.objects.create_user(username="bob", password="Correct-Horse-Battery-Staple-42")
        UserProfile.objects.filter(user=self.user).update(email_alerts=False, telegram_alerts=False)

    @mock.patch("notifications.tasks.send_telegram_message")
    def test_active_user_sees_digest_exactly_once(self, mock_telegram):
        send_daily_digest()
        visible = Alert.objects.filter(Q(user=self.user) | Q(user__isnull=True))
        self.assertEqual(visible.count(), 1)
        self.assertIsNone(visible.first().user)
        mock_telegram.assert_not_called()

    @mock.patch("notifications.tasks.send_telegram_message")
    def test_only_one_global_alert_created(self, mock_telegram):
        send_daily_digest()
        self.assertEqual(Alert.objects.count(), 1)
