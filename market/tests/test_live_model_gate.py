from datetime import date

from django.test import TestCase

from market.models import Exchange, MLModelVersion, PredictionSnapshot
from market.services.live_model_gate import suspend_critical_forward_models


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
