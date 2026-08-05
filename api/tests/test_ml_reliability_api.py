"""Admin-only read-only /api/ml-reliability/ endpoint (see
api.permissions.IsBazaarAdmin / accounts/roles.py — ML Reliability is an
Admin capability, not Staff's)."""
from datetime import date

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from market.models import Exchange, MLModelVersion, PredictionSnapshot
from market.services.ml_model import FEATURE_COLS
from market.services.reliability_metrics import MIN_SAMPLES_WATCH
from market.tests.test_reliability_report import _make_settled_snapshots


class MlReliabilityApiAccessTests(TestCase):
    def setUp(self):
        self.url = reverse("api_ml_reliability")

    def test_anonymous_is_forbidden(self):
        response = self.client.get(self.url)
        self.assertIn(response.status_code, (401, 403))

    def test_ordinary_authenticated_user_is_forbidden(self):
        User.objects.create_user(username="carol", password="Correct-Horse-Battery-Staple-42")
        self.client.login(username="carol", password="Correct-Horse-Battery-Staple-42")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_plain_staff_user_is_forbidden(self):
        staff = User.objects.create_user(username="staffapi", password="Correct-Horse-Battery-Staple-42")
        staff.is_staff = True
        staff.save(update_fields=["is_staff"])
        self.client.login(username="staffapi", password="Correct-Horse-Battery-Staple-42")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_admin_user_can_access(self):
        User.objects.create_superuser(username="adminapi", password="Correct-Horse-Battery-Staple-42")
        self.client.login(username="adminapi", password="Correct-Horse-Battery-Staple-42")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)


class MlReliabilityApiContentTests(TestCase):
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

        User.objects.create_superuser(username="adminapi2", password="Correct-Horse-Battery-Staple-42")
        self.client.login(username="adminapi2", password="Correct-Horse-Battery-Staple-42")

    def test_response_is_json_serializable_and_contains_assessments(self):
        response = self.client.get(reverse("api_ml_reliability"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("assessments", data)
        self.assertGreater(len(data["assessments"]), 0)
        row = data["assessments"][0]
        self.assertIn("status", row)
        self.assertIn("metrics", row)
        self.assertIn("recommendations", row)
        self.assertEqual(row["model_family"], "forward_return_rf")
