import numpy as np
import pandas as pd
from django.test import SimpleTestCase

from market.services.indicators import rsi


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
