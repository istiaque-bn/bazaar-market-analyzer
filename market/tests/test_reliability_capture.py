"""
ML Reliability Monitor — prediction capture tests.

Covers: immutable capture from AnalysisResult/NextDayCloseForecast,
duplicate-capture prevention, no-lookahead (a later change to the source
row must not retroactively change an already-captured snapshot), and
target-date computation using trading-session arithmetic (never calendar
days).
"""
from datetime import date

import numpy as np
import pandas as pd
from django.test import TestCase

from market.models import (
    AnalysisResult,
    Exchange,
    MLModelVersion,
    MarketHoliday,
    NextDayCloseForecast,
    PredictionSnapshot,
    PriceHistory,
    Stock,
)
from market.services.ml_model import FORWARD_HORIZON_TRADING_DAYS, FEATURE_COLS
from market.services.reliability_capture import (
    capture_forward_return_snapshots,
    capture_next_close_snapshots,
    capture_predictions,
    nth_trading_day_after,
)


def _make_price_history(stock, n=60, seed=0, end=date(2026, 6, 1)):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=end, periods=n, freq="C", weekmask="Sun Mon Tue Wed Thu")
    closes = 100 + np.cumsum(rng.normal(0.05, 1.0, n))
    closes = np.clip(closes, 5, None)
    PriceHistory.objects.bulk_create(
        PriceHistory(
            stock=stock, date=d.date(), open=float(c), high=float(c) + 0.5, low=float(c) - 0.5, close=float(c), volume=5000
        )
        for d, c in zip(dates, closes)
    )
    return dates[-1].date()


class NthTradingDayAfterTests(TestCase):
    def test_skips_weekends(self):
        # Thursday -> next trading day is Sunday (Fri/Sat are weekend for DSE/CSE)
        thursday = date(2026, 1, 1)  # a Thursday
        self.assertEqual(thursday.weekday(), 3)
        result = nth_trading_day_after(thursday, 1)
        self.assertEqual(result, date(2026, 1, 4))  # Sunday

    def test_skips_named_holiday(self):
        thursday = date(2026, 1, 1)
        MarketHoliday.objects.create(date=date(2026, 1, 4), name="Test Holiday")
        result = nth_trading_day_after(thursday, 1)
        self.assertEqual(result, date(2026, 1, 5))  # Monday, since Sunday is a holiday

    def test_counts_n_trading_sessions_not_calendar_days(self):
        start = date(2026, 1, 1)  # Thursday
        result = nth_trading_day_after(start, 5)
        # Manually count 5 Sun-Thu sessions after Jan 1, 2026
        d = start
        count = 0
        while count < 5:
            d += pd.Timedelta(days=1)
            d = d if isinstance(d, date) else d.date()
            if d.weekday() in (6, 0, 1, 2, 3):
                count += 1
        self.assertEqual(result, d)


class ForwardReturnCaptureTests(TestCase):
    def setUp(self):
        self.stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="CAP1", company_name="Cap One", is_active=True, group="A")
        self.as_of = _make_price_history(self.stock, n=60)
        self.version = MLModelVersion.objects.create(
            model_name="forward_return_rf",
            version="20260101-000000",
            exchange_scope="combined",
            status="active",
            is_active=True,
            data_cutoff=self.as_of,
            feature_schema=FEATURE_COLS,
            train_rows=500,
            fold_metadata=[{"train_class_balance": {"0": 10, "1": 30}}],
        )
        AnalysisResult.objects.create(
            stock=self.stock,
            as_of=self.as_of,
            action="BUY",
            score=40.0,
            confidence=0.6,
            ml_score=72.5,  # -> predicted_probability 0.725
            features={"close": 123.45},
        )

    def test_creates_one_snapshot_with_expected_fields(self):
        result = capture_forward_return_snapshots(as_of=self.as_of)
        self.assertEqual(result["created"], 1)
        snap = PredictionSnapshot.objects.get(model_family=PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF)
        self.assertAlmostEqual(snap.predicted_probability, 0.725)
        self.assertTrue(snap.predicted_class)
        self.assertEqual(snap.reference_close, 123.45)
        self.assertEqual(snap.model_version_tag, "20260101-000000")
        self.assertEqual(snap.horizon_trading_days, FORWARD_HORIZON_TRADING_DAYS)
        self.assertEqual(snap.stock_trading_code, "CAP1")
        self.assertTrue(snap.naive_baseline_class)  # majority class in fold_metadata was "1"
        self.assertEqual(snap.settlement_status, PredictionSnapshot.SettlementStatus.PENDING)
        self.assertIsNotNone(snap.target_date)
        self.assertGreater(snap.target_date, self.as_of)

    def test_recapturing_the_same_day_is_idempotent(self):
        capture_forward_return_snapshots(as_of=self.as_of)
        result2 = capture_forward_return_snapshots(as_of=self.as_of)
        self.assertEqual(result2["created"], 0)
        self.assertEqual(result2["already_captured"], 1)
        self.assertEqual(PredictionSnapshot.objects.count(), 1)

    def test_later_change_to_source_row_does_not_alter_the_snapshot(self):
        """No-lookahead / immutability: once captured, a snapshot must not
        silently change if the AnalysisResult it was captured from is
        later modified (e.g. by a subsequent day's re-analysis overwrite
        via update_or_create)."""
        capture_forward_return_snapshots(as_of=self.as_of)
        snap_before = PredictionSnapshot.objects.get()
        original_prob = snap_before.predicted_probability

        analysis = AnalysisResult.objects.get(stock=self.stock, as_of=self.as_of)
        analysis.ml_score = 5.0  # drastically different — simulates a later overwrite
        analysis.save(update_fields=["ml_score"])

        capture_forward_return_snapshots(as_of=self.as_of)  # re-run capture
        snap_after = PredictionSnapshot.objects.get()
        self.assertEqual(snap_after.predicted_probability, original_prob)

    def test_no_snapshot_without_an_active_model_version(self):
        MLModelVersion.objects.all().delete()
        result = capture_forward_return_snapshots(as_of=self.as_of)
        self.assertEqual(result["created"], 0)
        self.assertEqual(PredictionSnapshot.objects.count(), 0)

    def test_rows_without_ml_score_are_not_captured(self):
        AnalysisResult.objects.filter(stock=self.stock, as_of=self.as_of).update(ml_score=None)
        result = capture_forward_return_snapshots(as_of=self.as_of)
        self.assertEqual(result["created"], 0)


class NextCloseCaptureTests(TestCase):
    def setUp(self):
        self.stock = Stock.objects.create(exchange=Exchange.CSE, trading_code="CAP2", company_name="Cap Two", is_active=True, group="A")
        self.as_of = _make_price_history(self.stock, n=60)
        self.version = MLModelVersion.objects.create(
            model_name="next_close_rf",
            version="20260102-000000",
            exchange_scope="combined",
            status="active",
            is_active=True,
            data_cutoff=self.as_of,
            feature_schema=["rsi_14"],
            train_rows=500,
        )
        self.target = nth_trading_day_after(self.as_of, 1)
        NextDayCloseForecast.objects.create(
            stock=self.stock,
            as_of=self.as_of,
            target_date=self.target,
            last_close=100.0,
            predicted_close=101.5,
            predicted_return=0.015,
            confidence=0.6,
        )

    def test_creates_snapshot_from_forecast(self):
        result = capture_next_close_snapshots(as_of=self.as_of)
        self.assertEqual(result["created"], 1)
        snap = PredictionSnapshot.objects.get(model_family=PredictionSnapshot.ModelFamily.NEXT_CLOSE_RF)
        self.assertEqual(snap.reference_close, 100.0)
        self.assertEqual(snap.predicted_price, 101.5)
        self.assertEqual(snap.predicted_return, 0.015)
        self.assertEqual(snap.naive_baseline_return, 0.0)
        self.assertEqual(snap.target_date, self.target)
        self.assertEqual(snap.horizon_trading_days, 1)

    def test_idempotent_recapture(self):
        capture_next_close_snapshots(as_of=self.as_of)
        capture_next_close_snapshots(as_of=self.as_of)
        self.assertEqual(PredictionSnapshot.objects.count(), 1)


class CapturePredictionsCombinesBothFamiliesTests(TestCase):
    def test_capture_predictions_runs_both_families(self):
        stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="BOTH", company_name="Both Co", is_active=True, group="A")
        as_of = _make_price_history(stock, n=60)
        MLModelVersion.objects.create(
            model_name="forward_return_rf", version="v1", exchange_scope="combined", status="active",
            is_active=True, data_cutoff=as_of, feature_schema=FEATURE_COLS, train_rows=100,
        )
        MLModelVersion.objects.create(
            model_name="next_close_rf", version="v2", exchange_scope="combined", status="active",
            is_active=True, data_cutoff=as_of, feature_schema=["rsi_14"], train_rows=100,
        )
        AnalysisResult.objects.create(stock=stock, as_of=as_of, ml_score=60.0, features={"close": 50.0})
        NextDayCloseForecast.objects.create(
            stock=stock, as_of=as_of, target_date=as_of, last_close=50.0, predicted_close=51.0, predicted_return=0.02,
        )
        result = capture_predictions(as_of=as_of)
        self.assertTrue(result["ok"])
        self.assertEqual(result["forward_return_rf"]["created"], 1)
        self.assertEqual(result["next_close_rf"]["created"], 1)
        self.assertEqual(PredictionSnapshot.objects.count(), 2)
