from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase

from market.models import Exchange, PriceHistory, Stock
from market.services.close_learn import backfill_learn_from_history


class HistoricalBackfillSafetyTests(TestCase):
    def test_backfill_never_loads_a_later_trained_model(self):
        stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="SAFEHIST", company_name="Safe History")
        end = date.today()
        for number in range(100):
            day = end - timedelta(days=140 - number)
            close = 100 + number * 0.1
            PriceHistory.objects.create(stock=stock, date=day, open=close, high=close + 1, low=close - 1, close=close, volume=1000)

        with patch("market.services.close_learn._ml_one_day_return") as model_call, patch(
            "market.services.close_learn.train_next_close_model", return_value={"ok": True}
        ):
            backfill_learn_from_history(lookback_days=10, limit_stocks=1)

        model_call.assert_not_called()
