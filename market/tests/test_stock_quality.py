from datetime import date, timedelta

from django.test import TestCase

from market.models import Exchange, PriceHistory, Stock
from market.services.stock_quality import assess_stock_quality


class StockQualityAssessmentTests(TestCase):
    def test_bottom_decile_traded_value_is_labelled_limited(self):
        stocks = [
            Stock.objects.create(exchange=Exchange.DSE, trading_code=f"Q{i:02}", company_name=f"Quality {i}")
            for i in range(10)
        ]
        latest = date(2026, 3, 31)
        rows = []
        for index, stock in enumerate(stocks):
            value = 1.0 if index == 0 else float(100 + index)
            for offset in range(80):
                day = latest - timedelta(days=offset)
                rows.append(
                    PriceHistory(stock=stock, date=day, open=10, high=11, low=9, close=10, volume=1000, value=value)
                )
        PriceHistory.objects.bulk_create(rows)

        assessment = assess_stock_quality(stocks)

        self.assertTrue(assessment[stocks[0].id]["limited"])
        self.assertIn("Recent traded value is in the lowest 10%", assessment[stocks[0].id]["reasons"])
        self.assertFalse(assessment[stocks[-1].id]["limited"])

        searched_only = assess_stock_quality([stocks[0]])
        self.assertTrue(searched_only[stocks[0].id]["limited"])
        self.assertIn("Recent traded value is in the lowest 10%", searched_only[stocks[0].id]["reasons"])
