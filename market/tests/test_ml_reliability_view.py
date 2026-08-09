"""The Admin-only /ml-reliability/ dashboard page (see accounts/roles.py
— ML Reliability + pipeline/training controls are Admin capabilities,
not Staff's)."""
from datetime import date

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from market.models import Exchange, MLModelVersion, PredictionSnapshot, ReliabilityAssessment
from market.services.ml_model import FEATURE_COLS
from market.services.reliability_metrics import MIN_SAMPLES_WATCH
from market.tests.test_reliability_report import _make_settled_snapshots


class MlReliabilityViewAccessTests(TestCase):
    def setUp(self):
        self.url = reverse("ml_reliability")

    def test_anonymous_is_redirected_to_login(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_ordinary_authenticated_user_is_forbidden(self):
        User.objects.create_user(username="bob", password="Correct-Horse-Battery-Staple-42")
        self.client.login(username="bob", password="Correct-Horse-Battery-Staple-42")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_plain_staff_is_forbidden(self):
        staff = User.objects.create_user(username="staffer", password="Correct-Horse-Battery-Staple-42")
        staff.is_staff = True
        staff.save(update_fields=["is_staff"])
        self.client.login(username="staffer", password="Correct-Horse-Battery-Staple-42")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_admin_can_view_the_page_with_no_assessments_yet(self):
        User.objects.create_superuser(username="admin1", password="Correct-Horse-Battery-Staple-42")
        self.client.login(username="admin1", password="Correct-Horse-Battery-Staple-42")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"ML Reliability Monitor", response.content)
        self.assertIn(b"Training at a glance", response.content)
        self.assertIn(b"New price data becomes usable only after that result is known", response.content)
        self.assertIn(b"No assessments have run yet", response.content)


class MlReliabilityViewContentTests(TestCase):
    def setUp(self):
        from market.services.reliability_report import run_reliability_assessment

        MLModelVersion.objects.create(
            model_name="forward_return_rf", version="v1", exchange_scope="combined", status="active",
            is_active=True, data_cutoff=date(2026, 6, 1), train_rows=100, feature_schema=FEATURE_COLS,
        )
        _make_settled_snapshots(
            exchange=Exchange.DSE, model_version_tag="v1", family=PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF,
            n=MIN_SAMPLES_WATCH, skill_positive=True,
        )
        run_reliability_assessment(families=[PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF], windows=[MIN_SAMPLES_WATCH])

        User.objects.create_superuser(username="admin2", password="Correct-Horse-Battery-Staple-42")
        self.client.login(username="admin2", password="Correct-Horse-Battery-Staple-42")

    def test_shows_status_badge_and_recommendations(self):
        response = self.client.get(reverse("ml_reliability"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"HEALTHY", response.content)
        self.assertIn(b"Forward Return Classifier", response.content)

    def test_hides_assessments_for_disabled_exchange(self):
        assessment = ReliabilityAssessment.objects.first()
        assessment.exchange = Exchange.CSE
        assessment.save(update_fields=["exchange"])

        response = self.client.get(reverse("ml_reliability"))

        self.assertNotIn(b"Forward Return Classifier", response.content)

    def test_never_claims_safe_or_guaranteed(self):
        response = self.client.get(reverse("ml_reliability"))
        content_lower = response.content.lower()
        self.assertNotIn(b"guaranteed", content_lower)
        self.assertNotIn(b"is safe", content_lower)
