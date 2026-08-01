"""
A named holiday (market.models.MarketHoliday) must close the market for
its *entire* calendar day — from the first second, not just around normal
session hours — in both the live status banner (market_hours.session_status)
and the auto-sync cadence gate (autosync.is_market_hours).
"""
from datetime import date, datetime

from django.test import TestCase
from django.utils import timezone

from market.models import MarketHoliday
from market.services.autosync import is_market_hours
from market.services.market_hours import session_status


def _aware(y, m, d, hh, mm):
    return timezone.make_aware(datetime(y, m, d, hh, mm))


class SessionStatusHolidayTests(TestCase):
    def test_holiday_closed_all_day_including_session_hours(self):
        # 2026-08-12 is a Wednesday (a real trading weekday) with a named
        # holiday seeded by migration 0009/holiday_sync.
        MarketHoliday.objects.create(date=date(2026, 8, 12), name="Test National Holiday")

        early = session_status("DSE", now=_aware(2026, 8, 12, 0, 0))
        during_session = session_status("DSE", now=_aware(2026, 8, 12, 11, 0))
        late = session_status("DSE", now=_aware(2026, 8, 12, 23, 59))

        for status in (early, during_session, late):
            self.assertFalse(status["is_open"])
            self.assertIn("Test National Holiday", status["status"])

    def test_regular_trading_day_unaffected(self):
        # 2026-08-06 (Thursday) has no holiday seeded in these tests.
        during_session = session_status("DSE", now=_aware(2026, 8, 6, 11, 0))
        self.assertTrue(during_session["is_open"])
        self.assertEqual(during_session["status"], "Open")


class IsMarketHoursHolidayTests(TestCase):
    def test_holiday_during_normal_session_window_is_not_market_hours(self):
        MarketHoliday.objects.create(date=date(2026, 8, 12), name="Test National Holiday")
        self.assertFalse(is_market_hours(now=_aware(2026, 8, 12, 11, 0)))

    def test_non_holiday_trading_day_during_session_is_market_hours(self):
        self.assertTrue(is_market_hours(now=_aware(2026, 8, 6, 11, 0)))
