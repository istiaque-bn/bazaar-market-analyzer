import numpy as np
import pandas as pd
from django.test import SimpleTestCase

from market.services.predictor import action_from_score, predict_stock


def _trend_df(n=120, drift=0.3, seed=1, noise=0.4):
    rng = np.random.default_rng(seed)
    closes = 100 + np.cumsum(drift + rng.normal(0, noise, n))
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": closes + 0.5,
            "low": closes - 0.5,
            "close": closes,
            "volume": np.full(n, 5000),
        }
    )


class PredictStockInsufficientHistoryTests(SimpleTestCase):
    def test_none_df(self):
        pred = predict_stock(None)
        self.assertEqual(pred.action, "HOLD")
        self.assertEqual(pred.score, 0)
        self.assertFalse(pred.is_safe_buy)

    def test_short_df(self):
        pred = predict_stock(_trend_df(n=10))
        self.assertEqual(pred.action, "HOLD")
        self.assertEqual(pred.risk_level, "high")
        self.assertEqual(pred.confidence, 0.1)


class PredictStockShapeTests(SimpleTestCase):
    def test_returns_internally_consistent_prediction(self):
        pred = predict_stock(_trend_df(n=120, drift=0.3, seed=2))
        self.assertIn(pred.action, ("BUY", "SELL", "WATCH", "HOLD"))
        self.assertEqual(pred.action, action_from_score(pred.score))
        self.assertTrue(-100 <= pred.score <= 100)
        self.assertTrue(0.0 <= pred.confidence <= 1.0)
        self.assertIn(pred.risk_level, ("low", "medium", "high"))
        self.assertIsInstance(pred.patterns, list)
        self.assertIsInstance(pred.features, dict)

    def test_group_z_is_never_flagged_safe_even_with_strong_uptrend(self):
        # Strong, clean uptrend that would otherwise likely score BUY-high.
        pred = predict_stock(_trend_df(n=120, drift=0.8, noise=0.1, seed=3), group="Z")
        self.assertFalse(pred.is_safe_buy)
        self.assertEqual(pred.risk_level, "high")

    def test_is_safe_buy_implies_buy_action(self):
        for seed in range(5):
            pred = predict_stock(_trend_df(n=120, drift=0.8, noise=0.1, seed=seed))
            if pred.is_safe_buy:
                self.assertEqual(pred.action, "BUY")
                self.assertGreaterEqual(pred.score, 28)
                self.assertGreaterEqual(pred.confidence, 0.4)


class PredictStockPeRatioAdjustmentTests(SimpleTestCase):
    def test_low_pe_boosts_score_vs_no_pe(self):
        df = _trend_df(n=120, drift=0.1, seed=4)
        base = predict_stock(df, pe_ratio=None)
        with_low_pe = predict_stock(df, pe_ratio=8.0)
        # +6 unless already clipped at the +100 ceiling.
        if base.score < 94:
            self.assertGreater(with_low_pe.score, base.score)

    def test_high_pe_reduces_score_vs_no_pe(self):
        df = _trend_df(n=120, drift=0.1, seed=4)
        base = predict_stock(df, pe_ratio=None)
        with_high_pe = predict_stock(df, pe_ratio=45.0)
        if base.score > -92:
            self.assertLess(with_high_pe.score, base.score)
