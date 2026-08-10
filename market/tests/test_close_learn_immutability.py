from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase

from market.models import Exchange, NextDayCloseForecast, PriceHistory, Stock
from market.services.close_learn import generate_forecasts_for_as_of, liquid_stock_ids, next_trading_day


class CloseForecastImmutabilityTests(TestCase):
    def setUp(self):
        self.as_of = date(2026, 8, 6)
        self.stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="FROZEN", company_name="Frozen Ltd", last_price=100)
        for number in range(45):
            day = self.as_of - timedelta(days=44 - number)
            close = 80 + number
            PriceHistory.objects.create(stock=self.stock, date=day, open=close, high=close + 1, low=close - 1, close=close, volume=1000)

    def _prediction(self, close):
        return {"last_close": 124.0, "predicted_close": close, "predicted_return": 0.01, "raw_return": 0.01, "return_bias": 0.0, "confidence": 0.6, "method": "test", "samples": 40, "features": {}}

    @patch("market.services.reliability_capture.capture_next_close_snapshots", return_value={"ok": True})
    @patch("market.services.close_learn.forecast_next_close")
    @patch("market.services.close_learn.liquid_stock_ids", return_value=set())
    def test_retry_does_not_replace_existing_forecast(self, _liquid, forecast, _capture):
        forecast.return_value = self._prediction(125.0)
        generate_forecasts_for_as_of(as_of=self.as_of)
        forecast.return_value = self._prediction(130.0)
        generate_forecasts_for_as_of(as_of=self.as_of)

        row = NextDayCloseForecast.objects.get(stock=self.stock, target_date=next_trading_day(self.as_of))
        self.assertEqual(row.predicted_close, 125.0)
        self.assertEqual(NextDayCloseForecast.objects.count(), 1)

    def test_liquidity_ranking_excludes_future_volume(self):
        future_winner = Stock.objects.create(exchange=Exchange.DSE, trading_code="FUTURE", company_name="Future Ltd")
        past_winner = Stock.objects.create(exchange=Exchange.DSE, trading_code="PAST", company_name="Past Ltd")
        PriceHistory.objects.create(stock=future_winner, date=self.as_of, open=10, high=10, low=10, close=10, volume=10)
        PriceHistory.objects.create(stock=past_winner, date=self.as_of, open=10, high=10, low=10, close=10, volume=1000)
        PriceHistory.objects.create(stock=future_winner, date=self.as_of + timedelta(days=5), open=10, high=10, low=10, close=10, volume=999999)

        ranked = liquid_stock_ids(limit=1, as_of=self.as_of)

        self.assertEqual(ranked, {past_winner.id})

    @patch("market.services.reliability_capture.capture_next_close_snapshots", return_value={"ok": True})
    @patch("market.services.close_learn.forecast_next_close")
    @patch("market.services.close_learn.liquid_stock_ids", return_value=set())
    def test_forecast_input_never_contains_a_future_bar(self, _liquid, forecast, _capture):
        PriceHistory.objects.create(stock=self.stock, date=self.as_of + timedelta(days=1), open=999, high=999, low=999, close=999, volume=1000)
        seen = []
        def fake_prediction(frame, **_kwargs):
            seen.append(frame["date"].max().date())
            return self._prediction(125.0)
        forecast.side_effect = fake_prediction

        generate_forecasts_for_as_of(as_of=self.as_of)

        self.assertEqual(seen, [self.as_of])
