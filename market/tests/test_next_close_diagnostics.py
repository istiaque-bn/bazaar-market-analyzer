from datetime import date
from django.test import TestCase
from market.models import Exchange, PredictionSnapshot, Stock
from market.services.next_close_diagnostics import next_close_diagnostics

class NextCloseDiagnosticsTests(TestCase):
    def test_uses_only_settled_immutable_next_close_snapshots(self):
        stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="DIAG", company_name="Diagnostic")
        PredictionSnapshot.objects.create(model_family="next_close_rf", model_version_tag="baseline", stock=stock, stock_trading_code="DIAG", exchange=Exchange.DSE, data_cutoff_date=date(2026,1,1), horizon_trading_days=1, reference_close=100, target_date=date(2026,1,2), predicted_return=.01, predicted_price=101, confidence_value=.72, outcome_return=.02, outcome_price=102, settlement_status="settled")
        result = next_close_diagnostics()
        self.assertEqual(result["sample_count"], 1)
        self.assertEqual(result["confidence"][0]["bucket"], "70–79%")
        self.assertEqual(result["stocks"][0]["status"], "YELLOW")
