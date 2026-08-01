import numpy as np
import pandas as pd
from django.test import SimpleTestCase

from market.services.dse_fetcher import _safe_float, _safe_int
from market.services.ml_model import blend_score
from market.services.predictor import (
    _risk_from_vol,
    _trading_days_between,
    action_from_score,
    confidence_band,
    estimate_maturity_and_peak,
)


class ActionFromScoreTests(SimpleTestCase):
    def test_strong_positive_is_buy(self):
        self.assertEqual(action_from_score(25), "BUY")
        self.assertEqual(action_from_score(100), "BUY")

    def test_strong_negative_is_sell(self):
        self.assertEqual(action_from_score(-25), "SELL")
        self.assertEqual(action_from_score(-100), "SELL")

    def test_moderate_score_is_watch(self):
        self.assertEqual(action_from_score(12), "WATCH")
        self.assertEqual(action_from_score(-12), "WATCH")
        self.assertEqual(action_from_score(24), "WATCH")

    def test_weak_score_is_hold(self):
        self.assertEqual(action_from_score(0), "HOLD")
        self.assertEqual(action_from_score(11), "HOLD")
        self.assertEqual(action_from_score(-11), "HOLD")


class RiskFromVolTests(SimpleTestCase):
    def test_group_z_is_always_high_risk(self):
        self.assertEqual(_risk_from_vol(0.01, "Z"), "high")
        self.assertEqual(_risk_from_vol(None, "Z"), "high")

    def test_unknown_volatility_defaults_medium(self):
        self.assertEqual(_risk_from_vol(None, "A"), "medium")

    def test_low_and_high_volatility_boundaries(self):
        self.assertEqual(_risk_from_vol(0.10, "A"), "low")
        self.assertEqual(_risk_from_vol(0.50, "A"), "high")
        self.assertEqual(_risk_from_vol(0.30, "A"), "medium")


class ConfidenceBandTests(SimpleTestCase):
    def test_very_high_confidence(self):
        band = confidence_band(0.9)
        self.assertEqual(band["key"], "very_high")

    def test_very_low_confidence(self):
        band = confidence_band(0.05)
        self.assertEqual(band["key"], "very_low")

    def test_out_of_range_values_are_clipped(self):
        self.assertEqual(confidence_band(5.0)["key"], "very_high")
        self.assertEqual(confidence_band(-5.0)["key"], "very_low")


class TradingDaysBetweenTests(SimpleTestCase):
    def test_end_before_or_equal_start_is_zero(self):
        d = pd.Timestamp("2026-01-01")
        self.assertEqual(_trading_days_between(d, d), 0)
        self.assertEqual(_trading_days_between(d, d - pd.Timedelta(days=1)), 0)

    def test_counts_only_sun_thu(self):
        # 2026-01-01 is a Thursday; the following Fri/Sat must not count.
        start = pd.Timestamp("2026-01-01")
        end = pd.Timestamp("2026-01-04")  # Sunday
        self.assertEqual(_trading_days_between(start, end), 1)


class BlendScoreTests(SimpleTestCase):
    def test_none_ml_prob_returns_rule_score_unchanged(self):
        self.assertEqual(blend_score(42.0, None), 42.0)

    def test_neutral_ml_prob_leaves_score_mostly_rule_driven(self):
        # ml_prob=0.5 maps to ml_score=0, so blend is exactly 0.7*rule_score.
        self.assertAlmostEqual(blend_score(50.0, 0.5), 35.0)

    def test_result_is_clipped_to_valid_range(self):
        self.assertLessEqual(blend_score(100, 1.0), 100)
        self.assertGreaterEqual(blend_score(-100, 0.0), -100)


class SafeFloatIntTests(SimpleTestCase):
    def test_safe_float_parses_comma_thousands(self):
        self.assertEqual(_safe_float("1,234.5"), 1234.5)

    def test_safe_float_handles_placeholder_values(self):
        for placeholder in ("-", "N/A", "n/a", "--", ""):
            self.assertIsNone(_safe_float(placeholder))

    def test_safe_float_returns_default_on_garbage(self):
        self.assertEqual(_safe_float("not-a-number", default=0.0), 0.0)

    def test_safe_int_truncates_float_string(self):
        self.assertEqual(_safe_int("42.9"), 42)

    def test_safe_int_default_on_garbage(self):
        self.assertEqual(_safe_int("garbage", default=7), 7)


class EstimateMaturityAndPeakTests(SimpleTestCase):
    def test_short_history_returns_all_none(self):
        df = pd.DataFrame(
            {
                "date": pd.date_range("2026-01-01", periods=30),
                "open": np.full(30, 100.0),
                "high": np.full(30, 100.5),
                "low": np.full(30, 99.5),
                "close": np.full(30, 100.0),
                "volume": np.full(30, 1000),
            }
        )
        result = estimate_maturity_and_peak(df)
        self.assertIsNone(result["maturity_days"])
        self.assertIsNone(result["peak_days"])
        self.assertEqual(result["samples"], 0)

    def test_long_enough_history_returns_a_dict_shape(self):
        rng = np.random.default_rng(5)
        n = 150
        closes = 100 + np.cumsum(rng.normal(0, 1, n))
        df = pd.DataFrame(
            {
                "date": pd.date_range("2025-01-01", periods=n),
                "open": closes,
                "high": closes + 0.5,
                "low": closes - 0.5,
                "close": closes,
                "volume": np.full(n, 1000),
            }
        )
        result = estimate_maturity_and_peak(df)
        for key in ("maturity_days", "peak_days", "hit_rate", "avg_return", "samples", "return_p25", "return_p75", "downside_return"):
            self.assertIn(key, result)

    def test_short_history_has_none_range_and_downside(self):
        df = pd.DataFrame(
            {
                "date": pd.date_range("2026-01-01", periods=30),
                "open": np.full(30, 100.0),
                "high": np.full(30, 100.5),
                "low": np.full(30, 99.5),
                "close": np.full(30, 100.0),
                "volume": np.full(30, 1000),
            }
        )
        result = estimate_maturity_and_peak(df)
        self.assertIsNone(result["return_p25"])
        self.assertIsNone(result["return_p75"])
        self.assertIsNone(result["downside_return"])

    def test_downside_return_never_exceeds_avg_return_when_range_supported(self):
        """Each episode's trough return can never exceed its own peak
        return (the trough is the window's minimum, the peak its maximum),
        so the mean across episodes must preserve that ordering too."""
        rng = np.random.default_rng(11)
        n = 200
        closes = 100 + np.cumsum(rng.normal(0, 1, n))
        df = pd.DataFrame(
            {
                "date": pd.date_range("2025-01-01", periods=n),
                "open": closes,
                "high": closes + 0.5,
                "low": closes - 0.5,
                "close": closes,
                "volume": np.full(n, 1000),
            }
        )
        result = estimate_maturity_and_peak(df)
        if result["samples"] >= 10:
            self.assertIsNotNone(result["downside_return"])
            self.assertIsNotNone(result["return_p25"])
            self.assertLessEqual(result["downside_return"], result["avg_return"])
            self.assertLessEqual(result["return_p25"], result["return_p75"])
