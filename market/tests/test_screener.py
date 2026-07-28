from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from market.models import AnalysisResult, Exchange, SignalAction, Stock
from market.services.screener import potential_shares, safe_buys, screen_summary


class LatestAsOfFallbackTests(TestCase):
    """DSE/CSE only trade Sun-Thu, so `as_of` on the latest AnalysisResult is
    often not "today" (e.g. on a Friday/Saturday). The screener must fall
    back to the most recent as_of on record rather than requiring an exact
    match against timezone.localdate()."""

    def setUp(self):
        self.stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="TEST", company_name="Test Co")
        self.stale_as_of = timezone.localdate() - timedelta(days=2)
        AnalysisResult.objects.create(
            stock=self.stock,
            as_of=self.stale_as_of,
            action=SignalAction.BUY,
            score=50,
            confidence=0.6,
            is_safe_buy=True,
        )

    def test_potential_shares_returns_stale_as_of_result(self):
        results = list(potential_shares(min_score=25))
        self.assertEqual([r.stock_id for r in results], [self.stock.id])

    def test_safe_buys_returns_stale_as_of_result(self):
        results = list(safe_buys())
        self.assertEqual([r.stock_id for r in results], [self.stock.id])

    def test_screen_summary_uses_latest_as_of(self):
        summary = screen_summary()
        self.assertEqual(summary["as_of"], self.stale_as_of)
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["buy"], 1)

    def test_empty_when_no_analysis_exists(self):
        AnalysisResult.objects.all().delete()
        self.assertEqual(list(potential_shares()), [])
