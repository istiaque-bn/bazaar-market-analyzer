import json
from datetime import date, timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from market.models import Exchange, MLModelVersion, PredictionSnapshot
from market.services.ml_model import FEATURE_COLS
from market.services.reliability_metrics import MIN_SAMPLES_WATCH
from market.tests.test_reliability_report import _make_settled_snapshots


class AssessMlReliabilityCommandTests(TestCase):
    def setUp(self):
        MLModelVersion.objects.create(
            model_name="forward_return_rf", version="v1", exchange_scope="combined", status="active",
            is_active=True, data_cutoff=date(2026, 6, 1), train_rows=100, feature_schema=FEATURE_COLS,
        )
        _make_settled_snapshots(
            exchange=Exchange.DSE, model_version_tag="v1", family=PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF,
            n=MIN_SAMPLES_WATCH, skill_positive=True,
        )

    def test_plain_output_is_concise_and_readable(self):
        out = StringIO()
        call_command("assess_ml_reliability", "--model", "forward_return_rf", "--window", str(MIN_SAMPLES_WATCH), stdout=out)
        text = out.getvalue()
        self.assertIn("forward_return_rf", text)
        self.assertIn("HEALTHY", text)
        # Plain output must not just dump raw JSON blobs.
        self.assertNotIn('"metrics":', text)

    def test_json_output_contains_full_structured_assessment(self):
        out = StringIO()
        call_command("assess_ml_reliability", "--model", "forward_return_rf", "--window", str(MIN_SAMPLES_WATCH), "--json", stdout=out)
        data = json.loads(out.getvalue())
        self.assertIn("assessments", data)
        self.assertIn("metrics", data["assessments"][0])
        self.assertIn("drift", data["assessments"][0])
        self.assertIn("confidence_intervals", data["assessments"][0])

    def test_dry_run_makes_no_database_writes(self):
        from market.models import ReliabilityAssessment

        before = ReliabilityAssessment.objects.count()
        out = StringIO()
        call_command("assess_ml_reliability", "--dry-run", stdout=out)
        self.assertIn("DRY RUN", out.getvalue())
        self.assertEqual(ReliabilityAssessment.objects.count(), before)

    def test_settle_only_skips_the_assessment_report(self):
        out = StringIO()
        call_command("assess_ml_reliability", "--settle-only", stdout=out)
        text = out.getvalue()
        self.assertIn("Settled:", text)
        self.assertNotIn("HEALTHY", text)
        self.assertNotIn("CRITICAL", text)

    def test_exchange_filter_limits_output(self):
        out = StringIO()
        call_command("assess_ml_reliability", "--exchange", "CSE", "--window", str(MIN_SAMPLES_WATCH), "--json", stdout=out)
        data = json.loads(out.getvalue())
        exchanges = {a["exchange"] for a in data["assessments"]}
        self.assertEqual(exchanges, {"CSE"})
