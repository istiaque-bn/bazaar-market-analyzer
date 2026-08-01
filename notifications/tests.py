from unittest import mock

from django.contrib.auth.models import User
from django.db.models import Q
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token

from accounts.models import UserProfile
from market.models import MLModelVersion
from notifications.models import Alert, AlertChannel
from notifications.tasks import _digest_text, send_daily_digest


class DigestModelStatusTests(TestCase):
    """Phase 7: the digest must state plainly whether anything currently
    demonstrates a predictive edge, using the same signal_status logic
    as the web UI and API — not silently list "research candidates" as
    if they were backed by a validated model."""

    def test_digest_states_no_edge_when_nothing_deployed(self):
        text = _digest_text()
        self.assertIn("Model status: NO demonstrated predictive edge", text)

    def test_digest_states_demonstrated_edge_when_ml_active(self):
        MLModelVersion.objects.create(
            model_name="forward_return_rf",
            version="v1",
            exchange_scope="combined",
            status="active",
            is_active=True,
            data_cutoff=timezone.localdate(),
            train_rows=300,
            metrics={"skill_vs_baseline": {"majority_class": 0.08}, "model": {"direction_hit_rate": 0.58}},
        )
        text = _digest_text()
        self.assertIn("Model status: demonstrated edge", text)


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

    @mock.patch("notifications.tasks.send_telegram_message")
    def test_duplicate_trigger_is_idempotent_not_resent(self, mock_telegram):
        """A retry or a duplicate beat/worker fire for the same day must
        not re-send or duplicate the digest — proves task idempotency."""
        first = send_daily_digest()
        second = send_daily_digest()
        self.assertEqual(Alert.objects.count(), 1)
        self.assertIn("skipped", second)
        self.assertNotIn("skipped", first)


class AlertPrivacyTests(TestCase):
    """A user's personal Alert must never be visible to another user, on
    either the web view or the API — only their own + global (user=None)
    alerts."""

    def setUp(self):
        self.alice = User.objects.create_user(username="alice_alert", password="Correct-Horse-Battery-Staple-42")
        self.bob = User.objects.create_user(username="bob_alert", password="Correct-Horse-Battery-Staple-42")
        self.alice_alert = Alert.objects.create(
            user=self.alice, channel=AlertChannel.IN_APP, title="Alice-only", message="private to alice"
        )
        self.global_alert = Alert.objects.create(
            user=None, channel=AlertChannel.IN_APP, title="Global", message="everyone sees this"
        )

    def test_web_view_hides_other_users_personal_alert(self):
        self.client.login(username="bob_alert", password="Correct-Horse-Battery-Staple-42")
        response = self.client.get(reverse("alerts"))
        titles = {a.title for a in response.context["alerts"]}
        self.assertIn("Global", titles)
        self.assertNotIn("Alice-only", titles)

    def test_web_view_shows_own_personal_alert(self):
        self.client.login(username="alice_alert", password="Correct-Horse-Battery-Staple-42")
        response = self.client.get(reverse("alerts"))
        titles = {a.title for a in response.context["alerts"]}
        self.assertIn("Alice-only", titles)
        self.assertIn("Global", titles)

    def test_anonymous_sees_only_global_alerts(self):
        response = self.client.get(reverse("alerts"))
        titles = {a.title for a in response.context["alerts"]}
        self.assertEqual(titles, {"Global"})

    def test_api_hides_other_users_personal_alert(self):
        token = Token.objects.create(user=self.bob)
        response = self.client.get(reverse("api_alerts"), HTTP_AUTHORIZATION=f"Token {token.key}")
        titles = {row["title"] for row in response.json()["results"]} if "results" in response.json() else {
            row["title"] for row in response.json()
        }
        self.assertNotIn("Alice-only", titles)

    def test_api_shows_own_personal_alert(self):
        token = Token.objects.create(user=self.alice)
        response = self.client.get(reverse("api_alerts"), HTTP_AUTHORIZATION=f"Token {token.key}")
        body = response.json()
        titles = {row["title"] for row in (body["results"] if "results" in body else body)}
        self.assertIn("Alice-only", titles)
