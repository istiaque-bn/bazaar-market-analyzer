from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from market.models import AnalysisResult, Exchange, SignalAction, Stock
from market.services.screener import potential_shares, sell_candidates, top_by_sector


def _stock(code, exchange=Exchange.DSE, sector=""):
    return Stock.objects.create(exchange=exchange, trading_code=code, company_name=code, sector=sector)


def _analysis(stock, as_of, score, action=SignalAction.BUY, confidence=0.5):
    return AnalysisResult.objects.create(
        stock=stock, as_of=as_of, action=action, score=score, confidence=confidence
    )


class PotentialSharesSelectionTests(TestCase):
    def setUp(self):
        self.as_of = timezone.localdate()

    def test_below_min_score_excluded(self):
        s_low = _stock("LOW")
        s_high = _stock("HIGH")
        _analysis(s_low, self.as_of, score=10)
        _analysis(s_high, self.as_of, score=60)
        results = potential_shares(min_score=25)
        self.assertEqual([r.stock.trading_code for r in results], ["HIGH"])

    def test_exchange_filter(self):
        dse = _stock("DSEX", exchange=Exchange.DSE)
        cse = _stock("CSEX", exchange=Exchange.CSE)
        _analysis(dse, self.as_of, score=50)
        _analysis(cse, self.as_of, score=50)
        results = potential_shares(min_score=25, exchange=Exchange.CSE)
        self.assertEqual([r.stock.trading_code for r in results], ["CSEX"])

    def test_ordered_by_score_descending(self):
        s1 = _stock("A")
        s2 = _stock("B")
        s3 = _stock("C")
        _analysis(s1, self.as_of, score=30)
        _analysis(s2, self.as_of, score=90)
        _analysis(s3, self.as_of, score=60)
        results = potential_shares(min_score=0)
        self.assertEqual([r.score for r in results], [90, 60, 30])

    def test_limit_is_respected(self):
        for i in range(5):
            _analysis(_stock(f"S{i}"), self.as_of, score=50 + i)
        results = potential_shares(min_score=0, limit=2)
        self.assertEqual(len(results), 2)

    def test_only_latest_as_of_counts_not_older_analyses(self):
        older = self.as_of - timedelta(days=7)
        stale_stock = _stock("STALE")
        fresh_stock = _stock("FRESH")
        _analysis(stale_stock, older, score=95)  # would rank #1 by score alone
        _analysis(fresh_stock, self.as_of, score=30)
        results = potential_shares(min_score=0)
        # Only the latest as_of's rows are considered — the older, higher-scoring
        # row for a different as_of date must not appear.
        self.assertEqual([r.stock.trading_code for r in results], ["FRESH"])


class SellCandidatesSelectionTests(TestCase):
    def setUp(self):
        self.as_of = timezone.localdate()

    def test_only_sell_action_included(self):
        buy = _stock("BUYX")
        sell = _stock("SELLX")
        _analysis(buy, self.as_of, score=40, action=SignalAction.BUY)
        _analysis(sell, self.as_of, score=-40, action=SignalAction.SELL)
        results = sell_candidates()
        self.assertEqual([r.stock.trading_code for r in results], ["SELLX"])

    def test_ordered_worst_score_first(self):
        s1 = _stock("S1")
        s2 = _stock("S2")
        _analysis(s1, self.as_of, score=-10, action=SignalAction.SELL)
        _analysis(s2, self.as_of, score=-80, action=SignalAction.SELL)
        results = sell_candidates()
        self.assertEqual([r.score for r in results], [-80, -10])


class TopBySectorSelectionTests(TestCase):
    def setUp(self):
        self.as_of = timezone.localdate()

    def test_groups_by_sector_and_respects_per_sector_limit(self):
        for i in range(4):
            _analysis(_stock(f"BANK{i}", sector="Bank"), self.as_of, score=50 + i)
        for i in range(2):
            _analysis(_stock(f"PHARMA{i}", sector="Pharmaceuticals"), self.as_of, score=40 + i)
        buckets = top_by_sector(limit_per_sector=2)
        self.assertEqual(len(buckets["Bank"]), 2)
        self.assertEqual(len(buckets["Pharmaceuticals"]), 2)
        # Best-scoring Bank stocks kept (order_by -score before bucket fill).
        self.assertEqual({r.stock.trading_code for r in buckets["Bank"]}, {"BANK3", "BANK2"})

    def test_below_score_20_excluded(self):
        _analysis(_stock("WEAK", sector="Bank"), self.as_of, score=10)
        buckets = top_by_sector()
        self.assertNotIn("Bank", buckets)

    def test_missing_sector_grouped_as_other(self):
        _analysis(_stock("NOSECTOR", sector=""), self.as_of, score=50)
        buckets = top_by_sector()
        self.assertIn("Other", buckets)
