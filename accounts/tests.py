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

    def test_min_score_alert_above_boundary_rejected(self):
        response = self.client.post(self.url, {"min_score_alert": "150"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(UserProfile.objects.get(user=self.user).min_score_alert, 40)

    def test_min_score_alert_below_boundary_rejected(self):
        response = self.client.post(self.url, {"min_score_alert": "-5"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(UserProfile.objects.get(user=self.user).min_score_alert, 40)

    def test_unknown_exchange_rejected(self):
        response = self.client.post(self.url, {"min_score_alert": "40", "preferred_exchanges": "DSE,XYZ"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Unknown exchange")
        self.assertEqual(UserProfile.objects.get(user=self.user).preferred_exchanges, "DSE,CSE")

    def test_valid_exchange_choice_saves(self):
        response = self.client.post(self.url, {"min_score_alert": "40", "preferred_exchanges": "cse"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(UserProfile.objects.get(user=self.user).preferred_exchanges, "CSE")


class ProfileOwnershipTests(TestCase):
    """Each user's profile is auto-created and isolated — editing one
    user's settings must never touch another user's row."""

    def setUp(self):
        self.alice = User.objects.create_user(username="alice_p", password="Correct-Horse-Battery-Staple-42")
        self.bob = User.objects.create_user(username="bob_p", password="Correct-Horse-Battery-Staple-42")

    def test_signal_creates_one_profile_per_user(self):
        self.assertEqual(UserProfile.objects.filter(user=self.alice).count(), 1)
        self.assertEqual(UserProfile.objects.filter(user=self.bob).count(), 1)

    def test_editing_own_profile_does_not_touch_other_users(self):
        bob_before = UserProfile.objects.get(user=self.bob).min_score_alert
        self.client.login(username="alice_p", password="Correct-Horse-Battery-Staple-42")
        self.client.post(reverse("profile"), {"min_score_alert": "77"})
        self.assertEqual(UserProfile.objects.get(user=self.alice).min_score_alert, 77.0)
        self.assertEqual(UserProfile.objects.get(user=self.bob).min_score_alert, bob_before)

    def test_anonymous_cannot_view_profile(self):
        response = self.client.get(reverse("profile"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)
