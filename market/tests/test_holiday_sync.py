"""
market/services/holiday_sync.py parses DSE's holiday notice page and
upserts market.models.MarketHoliday. These tests exercise the parser
against a small fixture mimicking the real page's structure (no live
network calls — see market/tests/fixtures/) plus the upsert/idempotency
behavior and error handling of sync_holiday_calendar().
"""
from datetime import date
from unittest import mock

from django.test import TestCase

from market.models import MarketHoliday
from market.services.holiday_sync import HolidayPageParseError, fetch_dse_holidays, sync_holiday_calendar

FIXTURE_HTML = """
<html><body>
<p>List of Holidays: DSE will observe following Holidays during the Calendar
Year 2026 (January to December).</p>
<table>
  <tr><th>Name of Holidays</th><th>Date</th><th>Day(s)</th><th>No. of Days</th></tr>
  <tr><td>Shab-e-Barat (*)</td><td>04 February</td><td>Wednesday</td><td>1 day</td></tr>
  <tr><td>Election Holiday</td><td>11-12 February</td><td>Wednesday-Thursday</td><td>2 days</td></tr>
  <tr><td>Eid-ul-Fitr (*)</td><td>17 March - 23 March</td><td>Tuesday-Monday</td><td>7 days</td></tr>
  <tr><td>Trading Holiday (Bank Holiday)</td><td>31 December</td><td>Thursday</td><td>1 day</td></tr>
</table>
</body></html>
"""

MALFORMED_HTML = "<html><body><p>Nothing useful here.</p></body></html>"


class FetchDseHolidaysParsingTests(TestCase):
    def test_parses_year_and_all_row_shapes(self):
        year, entries = fetch_dse_holidays(html=FIXTURE_HTML)
        self.assertEqual(year, 2026)
        by_date = dict(entries)
        self.assertEqual(by_date[date(2026, 2, 4)], "Shab-e-Barat")
        self.assertEqual(by_date[date(2026, 2, 11)], "Election Holiday")
        self.assertEqual(by_date[date(2026, 2, 12)], "Election Holiday")
        self.assertEqual(by_date[date(2026, 3, 17)], "Eid-ul-Fitr")
        self.assertEqual(by_date[date(2026, 3, 23)], "Eid-ul-Fitr")
        self.assertEqual(by_date[date(2026, 12, 31)], "Trading Holiday (Bank Holiday)")

    def test_cross_month_range_expands_every_day_inclusive(self):
        _, entries = fetch_dse_holidays(html=FIXTURE_HTML)
        eid_dates = sorted(d for d, name in entries if name == "Eid-ul-Fitr")
        self.assertEqual(
            eid_dates,
            [date(2026, 3, d) for d in range(17, 24)],
        )

    def test_missing_year_text_raises(self):
        html = "<html><body><table><tr><th>Name of Holidays</th><th>Date</th></tr>" \
               "<tr><td>X</td><td>04 February</td></tr></table></body></html>"
        with self.assertRaises(HolidayPageParseError):
            fetch_dse_holidays(html=html)

    def test_missing_table_raises(self):
        with self.assertRaises(HolidayPageParseError):
            fetch_dse_holidays(html=MALFORMED_HTML)


class SyncHolidayCalendarTests(TestCase):
    def setUp(self):
        # The app ships a real seed migration (market.migrations.0009) with
        # its own holiday rows; clear it so these tests assert against a
        # known-empty baseline instead of tripping the unique-date
        # constraint or miscounting created/updated/unchanged.
        MarketHoliday.objects.all().delete()

    def test_upserts_and_skips_weekends(self):
        with mock.patch("market.services.holiday_sync.fetch_dse_holidays") as m:
            m.return_value = (2026, [
                (date(2026, 2, 4), "Shab-e-Barat"),  # Wednesday, kept
                (date(2026, 2, 21), "Shaheed Day"),  # Saturday, must be skipped
            ])
            result = sync_holiday_calendar()

        self.assertTrue(result["ok"])
        self.assertEqual(result["created"], 1)
        self.assertEqual(MarketHoliday.objects.filter(date=date(2026, 2, 4)).count(), 1)
        self.assertFalse(MarketHoliday.objects.filter(date=date(2026, 2, 21)).exists())

    def test_rerun_is_idempotent_and_renames_on_change(self):
        MarketHoliday.objects.create(date=date(2026, 2, 4), name="Old Name")
        with mock.patch("market.services.holiday_sync.fetch_dse_holidays") as m:
            m.return_value = (2026, [(date(2026, 2, 4), "Shab-e-Barat")])
            result = sync_holiday_calendar()

        self.assertEqual(result["created"], 0)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(MarketHoliday.objects.get(date=date(2026, 2, 4)).name, "Shab-e-Barat")

        with mock.patch("market.services.holiday_sync.fetch_dse_holidays") as m:
            m.return_value = (2026, [(date(2026, 2, 4), "Shab-e-Barat")])
            result2 = sync_holiday_calendar()
        self.assertEqual(result2["created"], 0)
        self.assertEqual(result2["updated"], 0)
        self.assertEqual(result2["unchanged"], 1)

    def test_specific_name_wins_over_umbrella_name_for_same_date(self):
        """Table order lists specific single-day holidays (e.g. a named
        sub-holiday) before the umbrella multi-day range they fall inside;
        the first name seen for a date should win."""
        with mock.patch("market.services.holiday_sync.fetch_dse_holidays") as m:
            m.return_value = (2026, [
                (date(2026, 3, 17), "Shab-e-Qadar"),
                (date(2026, 3, 17), "Eid-ul-Fitr"),
            ])
            sync_holiday_calendar()
        self.assertEqual(MarketHoliday.objects.get(date=date(2026, 3, 17)).name, "Shab-e-Qadar")

    def test_fetch_failure_reported_not_raised_and_does_not_touch_db(self):
        MarketHoliday.objects.create(date=date(2026, 1, 1), name="Existing")
        with mock.patch("market.services.holiday_sync.fetch_dse_holidays") as m:
            m.side_effect = HolidayPageParseError("site layout changed")
            result = sync_holiday_calendar()

        self.assertFalse(result["ok"])
        self.assertIn("site layout changed", result["error"])
        self.assertEqual(MarketHoliday.objects.count(), 1)
        self.assertEqual(MarketHoliday.objects.get().name, "Existing")
