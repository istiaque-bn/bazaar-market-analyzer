"""
Unit + view tests for trading_calendar: weekends and hand-maintained
MarketHoliday rows must be recognized as market closures, and the
stock_detail price-history table must label those days instead of
silently omitting them.
"""
from datetime import date, timedelta

from django.test import TestCase
from django.urls import reverse

from market.models import Exchange, MarketHoliday, PriceHistory, Stock
from market.services.trading_calendar import closure_reason, is_weekend


class ClosureReasonTests(TestCase):
    def test_friday_and_saturday_are_weekend(self):
        self.assertTrue(is_weekend(date(2026, 8, 7)))  # Friday
        self.assertTrue(is_weekend(date(2026, 8, 8)))  # Saturday
        self.assertEqual(closure_reason(date(2026, 8, 7)), "Weekend")
        self.assertEqual(closure_reason(date(2026, 8, 8)), "Weekend")

    def test_sunday_through_thursday_are_trading_days_by_default(self):
        for d in (date(2026, 8, 2), date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5), date(2026, 8, 6)):
            self.assertFalse(is_weekend(d))

    def test_seeded_holiday_is_recognized(self):
        # Independence Day 2026-03-26 is seeded by migration 0009; assert
        # against a fresh row instead, so this test doesn't depend on the
        # migration's exact seed list staying unchanged.
        MarketHoliday.objects.create(date=date(2026, 3, 5), name="Test National Holiday")
        self.assertEqual(closure_reason(date(2026, 3, 5)), "Test National Holiday")

    def test_holiday_on_a_weekend_reports_weekend_first(self):
        MarketHoliday.objects.create(date=date(2026, 8, 8), name="Coincidental Holiday")  # a Saturday
        self.assertEqual(closure_reason(date(2026, 8, 8)), "Weekend")

    def test_non_holiday_trading_day_has_no_closure(self):
        self.assertIsNone(closure_reason(date(2026, 8, 6)))  # Thursday, not in the seeded holiday list


class HistoryTableClosureLabelTests(TestCase):
    def _mk_stock(self, code="CALX"):
        return Stock.objects.create(exchange=Exchange.DSE, trading_code=code, company_name="Calendar Test Co")

    def test_weekend_gap_labeled_in_history_table(self):
        stock = self._mk_stock("CALX1")
        # 2026-08-06 (Thu) and 2026-08-09 (Sun) are real rows; 08-07/08-08
        # (Fri/Sat) have no PriceHistory row and must show as "Weekend".
        PriceHistory.objects.create(stock=stock, date=date(2026, 8, 6), open=10, high=11, low=9, close=10, volume=100)
        PriceHistory.objects.create(stock=stock, date=date(2026, 8, 9), open=10, high=11, low=9, close=10, volume=100)
        url = reverse("stock_detail", args=[stock.exchange, stock.trading_code]) + "?range=7d"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Market closed — Weekend")

    def test_named_holiday_labeled_in_history_table(self):
        stock = self._mk_stock("CALX2")
        # 2026-08-13 is a Thursday not in the seed migration's holiday list,
        # so this test's holiday is the only one in play.
        MarketHoliday.objects.create(date=date(2026, 8, 13), name="Test Holiday XYZ")
        PriceHistory.objects.create(stock=stock, date=date(2026, 8, 12), open=10, high=11, low=9, close=10, volume=100)
        PriceHistory.objects.create(stock=stock, date=date(2026, 8, 16), open=10, high=11, low=9, close=10, volume=100)
        url = reverse("stock_detail", args=[stock.exchange, stock.trading_code]) + "?range=7d"
        resp = self.client.get(url)
        self.assertContains(resp, "Market closed — Test Holiday XYZ")

    def test_genuine_data_gap_on_trading_day_not_mislabeled(self):
        """A missing row on an actual trading day (real data gap, not a
        closure) must not get a fabricated "Market closed" label — it
        should just be absent from the table, same as before this feature.
        Window kept to Sun-Tue (2026-08-02 to 2026-08-04) so it contains no
        real weekend/holiday, isolating the gap-day behavior."""
        stock = self._mk_stock("CALX3")
        PriceHistory.objects.create(stock=stock, date=date(2026, 8, 2), open=10, high=11, low=9, close=10, volume=100)
        PriceHistory.objects.create(stock=stock, date=date(2026, 8, 4), open=10, high=11, low=9, close=10, volume=100)
        url = reverse("stock_detail", args=[stock.exchange, stock.trading_code]) + "?range=3d"
        resp = self.client.get(url)
        self.assertNotContains(resp, "Market closed")
