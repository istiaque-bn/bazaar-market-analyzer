from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from market.models import AnalysisResult, Exchange, MLModelVersion, SignalAction, Stock


class AnalysisSerializerSignalStatusTests(TestCase):
    """Phase 7: API responses must carry the same honest model-status /
    edge information the web UI shows, not a bare score."""

    def setUp(self):
        self.stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="APIX1", company_name="API Test Co")
        self.analysis = AnalysisResult.objects.create(
            stock=self.stock,
            as_of=timezone.localdate(),
            action=SignalAction.BUY,
            is_safe_buy=True,
            score=40,
            confidence=0.6,
        )

    def test_stock_detail_api_includes_signal_status(self):
        url = reverse("api_stock_detail", args=[self.stock.exchange, self.stock.trading_code])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        status = resp.json()["analysis"]["signal_status"]
        self.assertIn("has_edge", status)
        self.assertIn("ml_model", status)
        self.assertIn("next_close_model", status)
        self.assertIn("liquidity", status)
        self.assertIn("invalidation", status)
        self.assertIn("signal_composition", status)
        self.assertFalse(status["has_edge"])

    def test_analysis_list_api_includes_signal_status_per_row(self):
        url = reverse("api_analysis")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["results"]
        self.assertTrue(rows)
        self.assertIn("signal_status", rows[0])

    def test_screener_api_includes_signal_status(self):
        url = reverse("api_screener")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        if data["potential"]:
            self.assertIn("signal_status", data["potential"][0])

    def test_signal_status_reflects_deployed_ml_model(self):
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
        url = reverse("api_stock_detail", args=[self.stock.exchange, self.stock.trading_code])
        resp = self.client.get(url)
        status = resp.json()["analysis"]["signal_status"]
        self.assertTrue(status["has_edge"])
        self.assertTrue(status["ml_model"]["deployed"])


class AnalysisListPerformanceTests(TestCase):
    """The signal-status field must be computed once per request (via
    serializer context), not once per row — otherwise a 100-row list
    response would re-run the next-close skill scan (a DB scan over up
    to 8000 rows) once per row instead of once total."""

    def _make_stocks(self, n, prefix):
        for i in range(n):
            stock = Stock.objects.create(exchange=Exchange.DSE, trading_code=f"{prefix}{i}")
            AnalysisResult.objects.create(stock=stock, as_of=timezone.localdate(), action=SignalAction.HOLD, score=0, confidence=0.2)

    def test_query_count_does_not_scale_with_row_count(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        url = reverse("api_analysis")

        self._make_stocks(3, "SMALL")
        with CaptureQueriesContext(connection) as small_ctx:
            resp_small = self.client.get(url)
        self.assertEqual(resp_small.status_code, 200)

        self._make_stocks(20, "BIG")
        with CaptureQueriesContext(connection) as big_ctx:
            resp_big = self.client.get(url)
        self.assertEqual(resp_big.status_code, 200)

        # 20 extra rows must not add anywhere near 20x the query count —
        # a per-row signal-status lookup would blow this bound.
        self.assertLess(len(big_ctx) - len(small_ctx), 5)
