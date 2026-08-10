from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from notifications.models import AdminReminder


class AdminReminderViewTests(TestCase):
    def setUp(self):
        self.url = reverse("admin_reminders")
        self.admin = User.objects.create_superuser(username="reminder_admin", password="Correct-Horse-Battery-Staple-42")

    def test_admin_can_create_a_reminder(self):
        self.client.login(username="reminder_admin", password="Correct-Horse-Battery-Staple-42")
        response = self.client.post(
            self.url,
            {
                "remind_on": (timezone.localdate() + timedelta(days=2)).isoformat(),
                "action": "Review the weekly paper-trading report.",
                "telegram_enabled": "on",
                "email_enabled": "on",
            },
        )
        self.assertRedirects(response, self.url)
        reminder = AdminReminder.objects.get()
        self.assertEqual(reminder.admin, self.admin)
        self.assertTrue(reminder.telegram_enabled)
        self.assertTrue(reminder.email_enabled)

    def test_regular_user_is_forbidden(self):
        User.objects.create_user(username="ordinary", password="Correct-Horse-Battery-Staple-42")
        self.client.login(username="ordinary", password="Correct-Horse-Battery-Staple-42")
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_past_date_is_rejected(self):
        self.client.login(username="reminder_admin", password="Correct-Horse-Battery-Staple-42")
        response = self.client.post(
            self.url,
            {"remind_on": (timezone.localdate() - timedelta(days=1)).isoformat(), "action": "Old reminder"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(AdminReminder.objects.count(), 0)
        self.assertContains(response, "Choose today or a future date.")
