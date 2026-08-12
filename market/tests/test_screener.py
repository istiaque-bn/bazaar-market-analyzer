from datetime import timedelta

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from market.models import AnalysisResult, Exchange, SignalAction, Stock
from market.services.screener import potential_shares, safe_buys, screen_summary, sentiment_label


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


class SentimentLabelTests(SimpleTestCase):
    def test_no_snapshot_yet_reports_no_data_not_neutral(self):
        result = sentiment_label(0, 0, 0)
        self.assertEqual(result["label"], "No data")
        self.assertEqual(result["score"], 0.0)
        self.assertEqual(result["total"], 0)

    def test_all_advancing_is_extremely_bullish(self):
        result = sentiment_label(advancers=200, decliners=0, unchanged=0)
        self.assertEqual(result["score"], 100.0)
        self.assertEqual(result["label"], "Extremely Bullish")

    def test_all_declining_is_extremely_bearish(self):
        result = sentiment_label(advancers=0, decliners=200, unchanged=0)
        self.assertEqual(result["score"], -100.0)
        self.assertEqual(result["label"], "Extremely Bearish")

    def test_even_split_is_neutral(self):
        result = sentiment_label(advancers=100, decliners=100, unchanged=50)
        self.assertEqual(result["score"], 0.0)
        self.assertEqual(result["label"], "Neutral")

    def test_bucket_boundaries_are_inclusive_on_the_lower_edge(self):
        # score = -60 exactly must land in "Bearish" (its bucket's lower
        # edge), not "Extremely Bearish" (which needs strictly < -60).
        result = sentiment_label(advancers=20, decliners=80, unchanged=0)
        self.assertEqual(result["score"], -60.0)
        self.assertEqual(result["label"], "Bearish")

    def test_totals_and_counts_are_echoed_back(self):
        result = sentiment_label(advancers=12, decliners=5, unchanged=3)
        self.assertEqual(result["advancers"], 12)
        self.assertEqual(result["decliners"], 5)
        self.assertEqual(result["unchanged"], 3)
        self.assertEqual(result["total"], 20)

    def test_needle_degrees_span_plus_minus_90_and_track_score_linearly(self):
        # The dashboard gauge renders this rotation server-side (not just
        # in JS) so a no-JS pageview still shows the correct needle
        # position instead of the CSS default's "fully bearish" pin.
        self.assertEqual(sentiment_label(100, 0, 0)["needle_deg"], 90.0)
        self.assertEqual(sentiment_label(0, 100, 0)["needle_deg"], -90.0)
        self.assertEqual(sentiment_label(50, 50, 0)["needle_deg"], 0.0)
        self.assertEqual(sentiment_label(0, 0, 0)["needle_deg"], 0.0)
