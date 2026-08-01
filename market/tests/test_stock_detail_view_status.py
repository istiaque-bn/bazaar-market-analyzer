"""
View-level tests for Phase 7: the stock_detail page must render honest
empty / stale / model-unavailable states, embed chart data safely (no
`|safe`-style raw JSON interpolation), and gate confident language on a
demonstrated edge.
"""
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from market.models import AnalysisResult, Exchange, MLModelVersion, NextDayCloseForecast, PriceHistory, SignalAction, Stock


def _mk_stock(code="VIEWX", **kwargs):
    return Stock.objects.create(exchange=Exchange.DSE, trading_code=code, company_name="View Test Co", **kwargs)


class EmptyStateTests(TestCase):
    def test_stock_with_no_analysis_shows_empty_state(self):
        stock = _mk_stock("EMPTY1")
        url = reverse("stock_detail", args=[stock.exchange, stock.trading_code])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Empty — no analysis on record yet")
        self.assertContains(resp, "Empty — no next-day close forecast yet")


class StaleDataStateTests(TestCase):
    def test_old_analysis_shows_stale_badge(self):
        stock = _mk_stock("STALE1")
        AnalysisResult.objects.create(
            stock=stock,
            as_of=timezone.localdate() - timedelta(days=20),
            action=SignalAction.BUY,
            is_safe_buy=True,
            score=40,
            confidence=0.6,
        )
        url = reverse("stock_detail", args=[stock.exchange, stock.trading_code])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Stale data")


class ModelUnavailableStateTests(TestCase):
    def test_no_ml_model_on_record_shows_not_deployed(self):
        stock = _mk_stock("NOEDGE1")
        AnalysisResult.objects.create(
            stock=stock,
            as_of=timezone.localdate(),
            action=SignalAction.HOLD,
            score=5,
            confidence=0.3,
        )
        url = reverse("stock_detail", args=[stock.exchange, stock.trading_code])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "No demonstrated predictive edge")
        self.assertContains(resp, "Not deployed for confident output")

    def test_research_candidate_badge_suppressed_without_edge(self):
        """is_safe_buy alone must not surface the validated-sounding
        "Experimental research candidate" badge when nothing demonstrates
        a positive, adequately-sampled edge — that would imply backing
        that doesn't exist."""
        stock = _mk_stock("NOEDGE2")
        AnalysisResult.objects.create(
            stock=stock,
            as_of=timezone.localdate(),
            action=SignalAction.BUY,
            is_safe_buy=True,
            score=40,
            confidence=0.6,
        )
        url = reverse("stock_detail", args=[stock.exchange, stock.trading_code])
        resp = self.client.get(url)
        content = resp.content.decode()
        self.assertNotIn("Experimental research candidate", content)
        self.assertIn("No demonstrated predictive edge", content)

    def test_research_candidate_badge_shown_with_demonstrated_edge(self):
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
        stock = _mk_stock("EDGE1")
        AnalysisResult.objects.create(
            stock=stock,
            as_of=timezone.localdate(),
            action=SignalAction.BUY,
            is_safe_buy=True,
            score=40,
            confidence=0.6,
        )
        url = reverse("stock_detail", args=[stock.exchange, stock.trading_code])
        resp = self.client.get(url)
        self.assertContains(resp, "Experimental research candidate")
        self.assertContains(resp, "Demonstrated edge")


class NextCloseForecastTickRoundingTests(TestCase):
    """The next-day close forecast is a model output, not a real observed
    trade — real DSE/CSE closes always land on a 0.10-taka tick, so the
    displayed prediction should too (see market/services/price_format.py)."""

    def test_predicted_close_displayed_rounded_to_nearest_tick(self):
        stock = _mk_stock("TICKFC1")
        NextDayCloseForecast.objects.create(
            stock=stock,
            as_of=timezone.localdate(),
            target_date=timezone.localdate() + timedelta(days=1),
            last_close=16.7,
            predicted_close=16.83,
            predicted_return=0.008,
        )
        url = reverse("stock_detail", args=[stock.exchange, stock.trading_code])
        resp = self.client.get(url)
        self.assertContains(resp, "predicted 16.80")
        self.assertNotContains(resp, "16.83")


class ChartJsonSafetyTests(TestCase):
    def test_no_raw_safe_filter_json_interpolation(self):
        stock = _mk_stock("SAFEJS1")
        base = timezone.localdate() - timedelta(days=5)
        PriceHistory.objects.bulk_create(
            [
                PriceHistory(stock=stock, date=base + timedelta(days=i), open=10, high=11, low=9, close=10, volume=100)
                for i in range(5)
            ]
        )
        url = reverse("stock_detail", args=[stock.exchange, stock.trading_code])
        resp = self.client.get(url)
        content = resp.content.decode()
        self.assertNotIn("|safe", content)
        self.assertIn('id="chart-data"', content)
        self.assertIn('type="application/json"', content)
        self.assertIn("JSON.parse(document.getElementById('chart-data')", content)


class DashboardEdgeBannerTests(TestCase):
    def test_dashboard_shows_no_edge_when_nothing_deployed(self):
        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "No demonstrated predictive edge")

    def test_dashboard_shows_demonstrated_edge_when_ml_active(self):
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
        resp = self.client.get(reverse("dashboard"))
        self.assertContains(resp, "Demonstrated edge")
