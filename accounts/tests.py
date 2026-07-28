from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from accounts.models import UserProfile


class ProfileMinScoreAlertTests(TestCase):
    """A non-numeric min_score_alert must show a form error, not 500."""

    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="Correct-Horse-Battery-Staple-42")
        self.client.force_login(self.user)
        self.url = reverse("profile")

    def test_non_numeric_min_score_alert_does_not_crash(self):
        response = self.client.post(self.url, {"min_score_alert": "not-a-number"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "must be a number")

    def test_valid_min_score_alert_saves(self):
        response = self.client.post(self.url, {"min_score_alert": "55"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(UserProfile.objects.get(user=self.user).min_score_alert, 55.0)
