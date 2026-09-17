from datetime import date

from django.test import TestCase

from market.models import Exchange, MLModelVersion, PredictionSnapshot, ReliabilityAssessment
from market.services.live_model_gate import activation_eligibility, suspend_critical_forward_models


class LiveModelGateTests(TestCase):
    def setUp(self):
        self.version = MLModelVersion.objects.create(
            model_name="forward_return_rf",
            version="gate-v1",
            exchange_scope=Exchange.DSE,
            status="active",
            is_active=True,
            data_cutoff=date(2026, 1, 1),
            train_rows=500,
        )

    def _assessment(self, *, status="critical", n=90):
        return {
            "model_family": PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF,
            "exchange": Exchange.DSE,
            "window_label": "90",
            "sample_count": n,
            "status": status,
            "model_version": self.version,
        }

    def test_critical_adequately_sampled_result_suspends_active_classifier(self):
        result = suspend_critical_forward_models([self._assessment()], dry_run=False)

        self.version.refresh_from_db()
        self.assertEqual(result[0]["action"], "suspended")
        self.assertFalse(self.version.is_active)
        self.assertEqual(self.version.status, "experimental")

    def test_dry_run_never_changes_model_status(self):
        result = suspend_critical_forward_models([self._assessment()], dry_run=True)

        self.version.refresh_from_db()
        self.assertEqual(result[0]["action"], "would_suspend")
        self.assertTrue(self.version.is_active)

    def test_watch_or_small_sample_does_not_suspend_model(self):
        self.assertEqual(suspend_critical_forward_models([self._assessment(status="watch")], dry_run=False), [])
        self.assertEqual(suspend_critical_forward_models([self._assessment(n=59)], dry_run=False), [])
        self.version.refresh_from_db()
        self.assertTrue(self.version.is_active)

    def test_suspends_combined_and_exchange_scoped_serving_candidates(self):
        combined = MLModelVersion.objects.create(
            model_name="forward_return_rf", version="gate-combined", exchange_scope="combined",
            status="active", is_active=True, data_cutoff=date(2026, 1, 1), train_rows=500,
        )
        result = suspend_critical_forward_models([self._assessment()], dry_run=False)

        self.version.refresh_from_db()
        combined.refresh_from_db()
        self.assertEqual(len(result), 2)
        self.assertFalse(self.version.is_active)
        self.assertFalse(combined.is_active)


class ActivationEligibilityTests(TestCase):
    def setUp(self):
        self.version = MLModelVersion.objects.create(
            model_name="forward_return_rf", version="candidate-v1", exchange_scope=Exchange.DSE,
            status="experimental", is_active=False, data_cutoff=date(2026, 1, 1), train_rows=500,
        )

    def test_no_live_assessment_fails_closed(self):
        allowed, reasons = activation_eligibility(self.version)
        self.assertFalse(allowed)
        self.assertIn("no 90-sample", reasons[0])

    def test_healthy_candidate_must_beat_both_baselines_after_costs(self):
        ReliabilityAssessment.objects.create(
            model_family=PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF,
            model_version=self.version,
            model_version_tag=self.version.version,
            exchange=Exchange.DSE,
            horizon_trading_days=10,
            window_label="90",
            window_size=90,
            sample_count=90,
            status=ReliabilityAssessment.Status.HEALTHY,
            metrics={
                "classification": {"skill_vs_baseline": {"rule_based": 0.1, "naive_majority_class": 0.05}},
                "economic": {"at_1x_cost": {"net_total_return_pct": 2.0}, "benchmark_relative_return_pct": 1.0},
            },
        )
        allowed, reasons = activation_eligibility(self.version)
        self.assertTrue(allowed, reasons)

    def test_positive_accuracy_without_after_cost_edge_is_rejected(self):
        ReliabilityAssessment.objects.create(
            model_family=PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF,
            model_version=self.version,
            model_version_tag=self.version.version,
            exchange=Exchange.DSE,
            horizon_trading_days=10,
            window_label="90",
            window_size=90,
            sample_count=90,
            status=ReliabilityAssessment.Status.HEALTHY,
            metrics={
                "classification": {"skill_vs_baseline": {"rule_based": 0.1, "naive_majority_class": 0.05}},
                "economic": {"at_1x_cost": {"net_total_return_pct": -1.0}, "benchmark_relative_return_pct": -0.5},
            },
        )
        allowed, reasons = activation_eligibility(self.version)
        self.assertFalse(allowed)
        self.assertTrue(any("after-cost" in reason for reason in reasons))
