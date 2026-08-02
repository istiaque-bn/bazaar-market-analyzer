"""
ML Reliability Monitor — settlement tests.

Covers: correct settlement against the exact target_date bar, holiday/
missing-price/suspended-volume handling, idempotent repeated execution,
and no look-ahead (settlement never uses a bar other than target_date's).
"""
from datetime import date, timedelta

from django.test import TestCase
from django.utils import timezone

from market.models import Exchange, PredictionSnapshot, PriceHistory, Stock
from market.services.reliability_settlement import MAX_PENDING_CALENDAR_DAYS, settle_predictions


def _snapshot(stock, *, data_cutoff, target_date, reference_close=100.0, predicted_return=0.02, **extra):
    defaults = dict(
        model_family=PredictionSnapshot.ModelFamily.NEXT_CLOSE_RF,
        model_version_tag="v1",
        stock=stock,
        stock_trading_code=stock.trading_code,
        exchange=stock.exchange,
        data_cutoff_date=data_cutoff,
        horizon_trading_days=1,
        target_date=target_date,
        reference_close=reference_close,
        predicted_return=predicted_return,
        predicted_price=reference_close * (1 + predicted_return),
    )
    defaults.update(extra)
    return PredictionSnapshot.objects.create(**defaults)


class SettlementCorrectnessTests(TestCase):
    def setUp(self):
        self.stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="SET1", company_name="Settle One")

    def test_settles_using_the_exact_target_date_bar(self):
        target = date(2026, 2, 5)
        PriceHistory.objects.create(stock=self.stock, date=target, open=100, high=102, low=99, close=105, volume=1000)
        # A bar on a later date must NOT be used even though it exists.
        PriceHistory.objects.create(stock=self.stock, date=target + timedelta(days=1), open=105, high=110, low=104, close=999, volume=1000)
        snap = _snapshot(self.stock, data_cutoff=target - timedelta(days=1), target_date=target, reference_close=100.0)

        result = settle_predictions(through_date=target)
        snap.refresh_from_db()
        self.assertEqual(result["settled"], 1)
        self.assertEqual(snap.settlement_status, PredictionSnapshot.SettlementStatus.SETTLED)
        self.assertEqual(snap.outcome_price, 105.0)
        self.assertAlmostEqual(snap.outcome_return, 0.05)
        self.assertTrue(snap.outcome_class)
        self.assertIsNotNone(snap.settled_at)

    def test_negative_outcome_return_is_class_false(self):
        target = date(2026, 2, 6)
        PriceHistory.objects.create(stock=self.stock, date=target, open=100, high=101, low=90, close=95, volume=1000)
        snap = _snapshot(self.stock, data_cutoff=target - timedelta(days=1), target_date=target, reference_close=100.0)
        settle_predictions(through_date=target)
        snap.refresh_from_db()
        self.assertFalse(snap.outcome_class)
        self.assertAlmostEqual(snap.outcome_return, -0.05)

    def test_future_target_date_stays_pending(self):
        target = timezone.localdate() + timedelta(days=30)
        snap = _snapshot(self.stock, data_cutoff=timezone.localdate(), target_date=target)
        result = settle_predictions(through_date=timezone.localdate())
        snap.refresh_from_db()
        self.assertEqual(result["settled"], 0)
        self.assertEqual(snap.settlement_status, PredictionSnapshot.SettlementStatus.PENDING)


class SettlementMissingDataTests(TestCase):
    def setUp(self):
        self.stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="SET2", company_name="Settle Two")

    def test_missing_bar_stays_pending_within_patience_window(self):
        target = date(2026, 2, 10)  # no PriceHistory bar written for this date
        snap = _snapshot(self.stock, data_cutoff=target - timedelta(days=1), target_date=target)
        result = settle_predictions(through_date=target + timedelta(days=1))
        snap.refresh_from_db()
        self.assertEqual(result["settled"], 0)
        self.assertEqual(result["excluded"], 0)
        self.assertEqual(snap.settlement_status, PredictionSnapshot.SettlementStatus.PENDING)

    def test_missing_bar_past_patience_window_is_excluded_with_reason(self):
        target = date(2026, 2, 10)
        snap = _snapshot(self.stock, data_cutoff=target - timedelta(days=1), target_date=target)
        result = settle_predictions(through_date=target + timedelta(days=MAX_PENDING_CALENDAR_DAYS + 1))
        snap.refresh_from_db()
        self.assertEqual(result["excluded"], 1)
        self.assertEqual(snap.settlement_status, PredictionSnapshot.SettlementStatus.EXCLUDED)
        self.assertEqual(snap.exclusion_reason, "no_settlement_data_available")
        self.assertIsNone(snap.outcome_return)  # outcome is never fabricated

    def test_zero_volume_bar_is_treated_as_suspended_and_eventually_excluded(self):
        target = date(2026, 2, 10)
        PriceHistory.objects.create(stock=self.stock, date=target, open=100, high=100, low=100, close=100, volume=0)
        snap = _snapshot(self.stock, data_cutoff=target - timedelta(days=1), target_date=target)
        result = settle_predictions(through_date=target + timedelta(days=MAX_PENDING_CALENDAR_DAYS + 1))
        snap.refresh_from_db()
        self.assertEqual(snap.settlement_status, PredictionSnapshot.SettlementStatus.EXCLUDED)
        self.assertEqual(snap.exclusion_reason, "suspended_or_zero_volume")

    def test_stock_deleted_is_excluded_immediately(self):
        target = date(2026, 2, 10)
        snap = _snapshot(self.stock, data_cutoff=target - timedelta(days=1), target_date=target)
        self.stock.delete()  # PredictionSnapshot.stock is SET_NULL
        snap.refresh_from_db()
        self.assertIsNone(snap.stock)
        result = settle_predictions(through_date=target)
        snap.refresh_from_db()
        self.assertEqual(result["excluded"], 1)
        self.assertEqual(snap.exclusion_reason, "stock_deleted")


class SettlementIdempotencyTests(TestCase):
    def setUp(self):
        self.stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="SET3", company_name="Settle Three")

    def test_rerunning_settlement_does_not_resettle_or_duplicate(self):
        target = date(2026, 2, 12)
        PriceHistory.objects.create(stock=self.stock, date=target, open=100, high=101, low=99, close=103, volume=1000)
        snap = _snapshot(self.stock, data_cutoff=target - timedelta(days=1), target_date=target, reference_close=100.0)

        result1 = settle_predictions(through_date=target)
        snap.refresh_from_db()
        first_settled_at = snap.settled_at
        self.assertEqual(result1["settled"], 1)

        result2 = settle_predictions(through_date=target)
        snap.refresh_from_db()
        self.assertEqual(result2["settled"], 0)  # nothing left to settle
        self.assertEqual(snap.settled_at, first_settled_at)  # untouched
        self.assertEqual(PredictionSnapshot.objects.count(), 1)
