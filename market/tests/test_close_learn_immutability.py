from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase

from market.models import Exchange, NextDayCloseForecast, PriceHistory, Stock
from market.services.close_learn import generate_forecasts_for_as_of, next_trading_day


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
