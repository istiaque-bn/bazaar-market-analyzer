import numpy as np
import pandas as pd
from django.test import SimpleTestCase

from market.services.indicators import (
    atr,
    bollinger,
    compute_indicators,
    latest_indicator_row,
    macd,
    rsi,
    support_resistance,
)


def _ohlcv_df(n=60, seed=1, start=100.0):
    rng = np.random.default_rng(seed)
    closes = start + np.cumsum(rng.normal(0, 1, n))
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": closes + 0.5,
            "low": closes - 0.5,
            "close": closes,
            "volume": np.full(n, 10000),
        }
    )


class RsiTests(SimpleTestCase):
    def test_all_gains_returns_100_not_nan(self):
        # 20 strictly increasing closes: zero losses in the window, so RS is
        # undefined by division rather than "no signal" (regression for the
        # avg_loss == 0 -> NaN bug).
        closes = pd.Series(range(100, 120))
        result = rsi(closes, window=14)
        self.assertFalse(result.tail(1).isna().any())
        self.assertAlmostEqual(result.iloc[-1], 100.0)

    def test_flat_price_returns_50_not_nan(self):
        closes = pd.Series([100.0] * 20)
        result = rsi(closes, window=14)
        self.assertFalse(result.tail(1).isna().any())
        self.assertAlmostEqual(result.iloc[-1], 50.0)

    def test_mixed_moves_bounded_0_100(self):
        rng = np.random.default_rng(42)
        closes = pd.Series(100 + np.cumsum(rng.normal(0, 1, 60)))
        result = rsi(closes, window=14).dropna()
        self.assertTrue((result >= 0).all())
        self.assertTrue((result <= 100).all())


class MacdTests(SimpleTestCase):
    def test_histogram_equals_line_minus_signal(self):
        closes = pd.Series(100 + np.cumsum(np.random.default_rng(7).normal(0, 1, 60)))
        line, signal, hist = macd(closes)
        pd.testing.assert_series_equal(hist, line - signal, check_names=False)

    def test_flat_series_has_zero_macd(self):
        closes = pd.Series([50.0] * 40)
        line, signal, hist = macd(closes)
        self.assertTrue(np.allclose(line, 0.0))
        self.assertTrue(np.allclose(hist, 0.0))


class BollingerTests(SimpleTestCase):
    def test_bands_ordered_upper_mid_lower(self):
        closes = pd.Series(100 + np.cumsum(np.random.default_rng(3).normal(0, 1, 40)))
        upper, mid, lower = bollinger(closes)
        valid = upper.notna() & lower.notna()
        self.assertTrue((upper[valid] >= mid[valid]).all())
        self.assertTrue((mid[valid] >= lower[valid]).all())

    def test_constant_series_bands_collapse_to_price(self):
        closes = pd.Series([75.0] * 30)
        upper, mid, lower = bollinger(closes)
        tail = slice(20, 30)
        self.assertTrue(np.allclose(upper[tail], 75.0))
        self.assertTrue(np.allclose(lower[tail], 75.0))
        self.assertTrue(np.allclose(mid[tail], 75.0))


class AtrTests(SimpleTestCase):
    def test_atr_never_negative(self):
        df = _ohlcv_df(n=40)
        result = atr(df)
        self.assertTrue((result >= 0).all())

    def test_atr_zero_when_no_movement(self):
        df = pd.DataFrame({"high": [10.0] * 20, "low": [10.0] * 20, "close": [10.0] * 20})
        result = atr(df)
        self.assertTrue(np.allclose(result, 0.0))


class SupportResistanceTests(SimpleTestCase):
    def test_empty_df_returns_none_none(self):
        self.assertEqual(support_resistance(pd.DataFrame()), (None, None))

    def test_returns_min_low_max_high_within_lookback(self):
        df = pd.DataFrame({"low": [10, 8, 12, 9, 11], "high": [15, 16, 14, 20, 13]})
        support, resistance = support_resistance(df, lookback=5)
        self.assertEqual(support, 8)
        self.assertEqual(resistance, 20)

    def test_lookback_excludes_older_rows(self):
        # 10 rows; lookback=3 must only consider the last 3.
        df = pd.DataFrame({"low": [1, 1, 1, 1, 1, 1, 1, 50, 60, 55], "high": [2] * 7 + [100, 90, 95]})
        support, resistance = support_resistance(df, lookback=3)
        self.assertEqual(support, 50)
        self.assertEqual(resistance, 100)


class ComputeIndicatorsEdgeCaseTests(SimpleTestCase):
    def test_empty_df_returned_unchanged(self):
        df = pd.DataFrame()
        result = compute_indicators(df)
        self.assertTrue(result.empty)

    def test_short_df_returned_unchanged_no_indicator_columns(self):
        """compute_indicators silently no-ops below 5 rows — callers must
        not assume indicator columns always exist."""
        df = _ohlcv_df(n=4)
        result = compute_indicators(df)
        self.assertNotIn("rsi_14", result.columns)
        self.assertEqual(len(result), 4)

    def test_five_rows_adds_indicator_columns(self):
        df = _ohlcv_df(n=5)
        result = compute_indicators(df)
        for col in ("sma_20", "rsi_14", "macd", "bb_upper", "atr_14"):
            self.assertIn(col, result.columns)


class LatestIndicatorRowTests(SimpleTestCase):
    def test_empty_df_returns_empty_dict(self):
        self.assertEqual(latest_indicator_row(pd.DataFrame()), {})

    def test_returns_scalar_values_for_last_row(self):
        df = _ohlcv_df(n=60)
        result = latest_indicator_row(df)
        self.assertIn("rsi_14", result)
        self.assertIn("support", result)
        self.assertIn("resistance", result)
        self.assertIsInstance(result["date"], object)
