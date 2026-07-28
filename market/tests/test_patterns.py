import pandas as pd
from django.test import SimpleTestCase

from market.services.patterns import detect_patterns


def _tight_range_df(last_close: float) -> pd.DataFrame:
    """40+ day OHLCV series with a ~3% 40-day range (support ~98, resistance
    ~101), so support*1.02 and resistance*0.98 overlap and any close between
    them would previously trigger both "Near Support" and "Near Resistance"
    at once."""
    n = 45
    dates = pd.date_range("2024-01-01", periods=n, freq="D")
    closes = [99.5] * n
    closes[5] = 99.5  # ensure low comes from an explicit row below
    rows = []
    for i in range(n):
        close = closes[i]
        low = close - 0.3
        high = close + 0.3
        rows.append({"date": dates[i], "open": close, "high": high, "low": low, "close": close, "volume": 10000})
    rows[10]["low"] = 98.0  # 40-day support
    rows[20]["high"] = 101.0  # 40-day resistance
    rows[-1]["close"] = last_close
    rows[-1]["high"] = max(rows[-1]["high"], last_close)
    rows[-1]["low"] = min(rows[-1]["low"], last_close)
    return pd.DataFrame(rows)


class NearSupportResistanceTests(SimpleTestCase):
    def test_tight_range_does_not_fire_both_at_once(self):
        # last_close=99.4 sits inside both the "near support" (<=98*1.02=99.96)
        # and "near resistance" (>=101*0.98=98.98) bands, but is closer to
        # support (1.4 away) than resistance (1.6 away).
        df = _tight_range_df(last_close=99.4)
        patterns = detect_patterns(df)
        names = {p["name"] for p in patterns}
        self.assertIn("Near Support", names)
        self.assertNotIn("Near Resistance", names)

    def test_tight_range_picks_resistance_when_closer(self):
        df = _tight_range_df(last_close=99.9)
        patterns = detect_patterns(df)
        names = {p["name"] for p in patterns}
        self.assertIn("Near Resistance", names)
        self.assertNotIn("Near Support", names)

    def test_short_history_returns_no_patterns(self):
        df = _tight_range_df(last_close=99.5).head(10)
        self.assertEqual(detect_patterns(df), [])
