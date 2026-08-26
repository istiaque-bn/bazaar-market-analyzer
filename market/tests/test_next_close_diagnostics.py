from datetime import date
from django.test import TestCase
from market.models import Exchange, PredictionSnapshot, Stock
from market.services.next_close_diagnostics import abstention_policy_comparison, next_close_diagnostics

class NextCloseDiagnosticsTests(TestCase):
    def test_uses_only_settled_immutable_next_close_snapshots(self):
        stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="DIAG", company_name="Diagnostic")
        PredictionSnapshot.objects.create(model_family="next_close_rf", model_version_tag="baseline", stock=stock, stock_trading_code="DIAG", exchange=Exchange.DSE, data_cutoff_date=date(2026,1,1), horizon_trading_days=1, reference_close=100, target_date=date(2026,1,2), predicted_return=.01, predicted_price=101, confidence_value=.72, outcome_return=.02, outcome_price=102, settlement_status="settled")
        result = next_close_diagnostics()
        self.assertEqual(result["sample_count"], 1)
        self.assertEqual(result["confidence"][0]["bucket"], "70–79%")
        self.assertEqual(result["stocks"][0]["status"], "YELLOW")
        self.assertEqual(result["versions"][0]["version"], "baseline")
        self.assertEqual(result["volatility_regimes"][0]["regime"], "Context not captured")

    def test_policy_requires_live_skill_and_direction_lift_before_recommending_change(self):
        stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="POLICY", company_name="Policy")
        rows = []
        for index in range(35):
            rows.append(PredictionSnapshot.objects.create(
                model_family="next_close_rf", model_version_tag="candidate", stock=stock,
                stock_trading_code=f"POLICY-BAD-{index}", exchange=Exchange.DSE,
                data_cutoff_date=date(2026, 2, 1), horizon_trading_days=1,
                reference_close=100, target_date=date(2026, 2, 2),
                predicted_return=.01, predicted_price=101, confidence_value=.75,
                outcome_return=.02, outcome_price=102, settlement_status="settled",
            ))
        for index in range(5):
            rows.append(PredictionSnapshot.objects.create(
                model_family="next_close_rf", model_version_tag="candidate", stock=stock,
                stock_trading_code=f"POLICY-{index}", exchange=Exchange.DSE,
                data_cutoff_date=date(2026, 2, 1), horizon_trading_days=1,
                reference_close=100, target_date=date(2026, 2, 2),
                predicted_return=.01, predicted_price=101, confidence_value=.30,
                outcome_return=-.03, outcome_price=97, settlement_status="settled",
            ))
        policy = abstention_policy_comparison(rows)
        self.assertIsNotNone(policy["recommended"])
        self.assertGreaterEqual(policy["recommended"]["confidence_threshold"], .55)
        self.assertGreater(policy["recommended"]["skill_vs_naive"], 0)
