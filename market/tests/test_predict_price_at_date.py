from datetime import date, timedelta

import numpy as np
import pandas as pd
from django.test import SimpleTestCase

from market.services.predictor import predict_price_at_date


def _df(n=40, start=date(2025, 11, 1), seed=11, skip_dates=()):
    dates = [start + timedelta(days=i) for i in range(n)]
    dates = [d for d in dates if d not in skip_dates]
    rng = np.random.default_rng(seed)
    closes = 100 + np.cumsum(rng.normal(0, 1, len(dates)))
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": closes + 0.5,
            "low": closes - 0.5,
            "close": closes,
            "volume": np.full(len(dates), 5000),
        }
    )


class InsufficientHistoryTests(SimpleTestCase):
    def test_empty_df(self):
        result = predict_price_at_date(pd.DataFrame(), date(2026, 1, 1))
        self.assertFalse(result["ok"])
        self.assertIn("Insufficient", result["error"])

    def test_none_df(self):
        result = predict_price_at_date(None, date(2026, 1, 1))
        self.assertFalse(result["ok"])

    def test_fewer_than_30_rows(self):
        df = _df(n=20)
        result = predict_price_at_date(df, df["date"].max() + timedelta(days=5))
        self.assertFalse(result["ok"])
        self.assertIn("Insufficient", result["error"])


class KnownTargetTests(SimpleTestCase):
    """A target date already in the recorded history returns the actual
    close, not a probabilistic estimate."""

    def test_exact_historical_bar_returns_actual_close(self):
        df = _df(n=40)
        target = df.iloc[20]["date"]
        expected_close = df.iloc[20]["close"]
        result = predict_price_at_date(df, target)
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "actual")
        self.assertEqual(result["predicted_price"], round(float(expected_close), 2))
        self.assertEqual(result["horizon_trading_days"], 0)
        self.assertEqual(result["confidence"], 0.99)

    def test_most_recent_bar_is_also_actual(self):
        df = _df(n=40)
        last_date = df["date"].max()
        result = predict_price_at_date(df, last_date)
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "actual")


class MissingHistoricalBarTests(SimpleTestCase):
    """A past date with no recorded bar (holiday/gap) must not be silently
    treated as a forecast target — it's a data-availability error."""

    def test_gap_date_before_last_bar_is_rejected(self):
        gap = date(2025, 11, 15)
        df = _df(n=40, skip_dates=(gap,))
        self.assertNotIn(gap, set(df["date"]))
        result = predict_price_at_date(df, gap)
        self.assertFalse(result["ok"])
        self.assertIn("No price bar", result["error"])


class UnknownFutureTargetTests(SimpleTestCase):
    """The core "unknown future target" construction: horizon computed in
    BD trading days (Sun-Thu), forecast mode, bounded to ~6 months."""

    def test_future_target_within_range_is_a_forecast(self):
        df = _df(n=60)
        last_date = df["date"].max()
        target = last_date + timedelta(days=10)
        result = predict_price_at_date(df, target)
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "forecast")
        self.assertGreater(result["horizon_trading_days"], 0)
        self.assertIn("price_low", result)
        self.assertIn("price_high", result)
        self.assertLessEqual(result["price_low"], result["price_high"])
        self.assertIn("confidence", result)
        self.assertTrue(0.0 <= result["confidence"] <= 1.0)

    def test_far_future_target_beyond_180_trading_days_rejected(self):
        df = _df(n=60)
        last_date = df["date"].max()
        target = last_date + timedelta(days=300)  # ~214 BD trading days
        result = predict_price_at_date(df, target)
        self.assertFalse(result["ok"])
        self.assertIn("6 months", result["error"])
        self.assertGreater(result["horizon_trading_days"], 180)

    def test_next_trading_day_forecast_does_not_crash_without_close_learn_state(self):
        """horizon==1 tries to blend in close-learn state; with none set up
        yet it must fall back gracefully, not raise."""
        df = _df(n=60)
        last_date = df["date"].max()
        # Smallest step forward that still resolves to horizon >= 1.
        target = last_date + timedelta(days=1)
        result = predict_price_at_date(df, target)
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "forecast")

    def test_predicted_price_is_positive_and_reasonable(self):
        df = _df(n=80, seed=99)
        last_close = float(df.iloc[-1]["close"])
        target = df["date"].max() + timedelta(days=15)
        result = predict_price_at_date(df, target)
        self.assertTrue(result["ok"])
        # Sanity bound: short-horizon forecast shouldn't blow up wildly
        # away from the last known price.
        self.assertGreater(result["predicted_price"], last_close * 0.5)
        self.assertLess(result["predicted_price"], last_close * 1.5)

    def test_forecast_prices_are_tick_aligned(self):
        """Real DSE/CSE trades only land on 0.10-taka increments — a
        forecast price like 16.83 can't be a real future price, so
        predicted_price/price_low/price_high must round to the nearest
        tick (see market/services/price_format.py)."""
        df = _df(n=80, seed=7)
        target = df["date"].max() + timedelta(days=15)
        result = predict_price_at_date(df, target)
        self.assertTrue(result["ok"])
        for key in ("predicted_price", "price_low", "price_high"):
            cents = round(result[key] * 100)
            self.assertEqual(cents % 10, 0, f"{key}={result[key]} is not tick-aligned")

    def test_downside_scenario_present_and_below_low_with_enough_analogues(self):
        df = _df(n=60)
        target = df["date"].max() + timedelta(days=10)
        result = predict_price_at_date(df, target)
        self.assertTrue(result["ok"])
        self.assertIn("price_downside", result)
        self.assertIn("downside_note", result)
        if result["price_downside"] is not None:
            self.assertLessEqual(result["price_downside"], result["price_low"])

    def test_actual_historical_bar_has_no_downside_scenario(self):
        df = _df(n=60)
        target = df.iloc[10]["date"]
        result = predict_price_at_date(df, target)
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "actual")
        self.assertIsNone(result["price_downside"])
        self.assertIn("not a forecast", result["downside_note"])
