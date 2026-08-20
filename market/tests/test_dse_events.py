"""
market/services/dse_events.py parses DSE's AGM/EGM/Record Date PDF and
upserts market.models.MarketEvent. Pure parsing helpers are tested
directly; parse_agm_egm_pdf is tested against a mocked pdfplumber (no
real PDF bytes needed — see market.tests.test_holiday_sync for the same
pattern against BeautifulSoup/HTML instead); sync_dse_agm_egm_events is
tested against a mocked parse step, exercising real DB upsert/dedup
behavior (no live network calls anywhere in this file).
"""
from datetime import date
from unittest import mock

from django.test import TestCase

from market.models import MarketEvent
from market.services.dse_events import (
    AGMPageParseError,
    _clean,
    _clean_venue,
    _details_text,
    _event_type_for_purpose,
    _parse_notice_date,
    parse_agm_egm_pdf,
    sync_dse_agm_egm_events,
)


class CleanAndDateParsingTests(TestCase):
    def test_clean_collapses_whitespace_and_newlines(self):
        self.assertEqual(_clean("aamra technologies\nlimited"), "aamra technologies limited")
        self.assertEqual(_clean("  a   b  "), "a b")
        self.assertEqual(_clean(None), "")

    def test_parses_real_date(self):
        self.assertEqual(_parse_notice_date("14-Sep-26"), date(2026, 9, 14))
        self.assertEqual(_parse_notice_date("9-Jun-26"), date(2026, 6, 9))

    def test_placeholder_text_returns_none_not_raise(self):
        for text in ("Will be notified later", "Postponed", "-", "", "Will be notified later.", None):
            self.assertIsNone(_parse_notice_date(text))

    def test_trailing_period_and_whitespace_tolerated(self):
        self.assertEqual(_parse_notice_date(" 16-Aug-26. "), date(2026, 8, 16))


class EventTypeHeuristicTests(TestCase):
    def test_defaults_to_agm(self):
        self.assertEqual(_event_type_for_purpose("10% Cash Dividend"), MarketEvent.EventType.AGM)

    def test_egm_keyword_wins(self):
        self.assertEqual(
            _event_type_for_purpose("For the purpose of considering amendments to the Articles of Association."),
            MarketEvent.EventType.EGM,
        )
        self.assertEqual(_event_type_for_purpose("Extraordinary General Meeting"), MarketEvent.EventType.EGM)


class DetailsTextTests(TestCase):
    def test_omits_empty_purpose(self):
        self.assertEqual(_details_text(), "")

    def test_wraps_purpose_when_present(self):
        text = _details_text(purpose="10% Cash Dividend")
        self.assertEqual(text, "Purpose: 10% Cash Dividend")


class CleanVenueTests(TestCase):
    def test_strips_source_own_venue_label_to_avoid_double_labeling(self):
        for raw in (
            "Venue/Mode: Digital Platform",
            "Venue: Digital Platform",
            "Mode of AGM: Digital Platform",
            "Mode: Digital Platform",
            "Venue/Platform: Digital Platform",
        ):
            self.assertEqual(_clean_venue(raw), "Digital Platform", msg=raw)

    def test_venue_without_a_label_prefix_is_untouched(self):
        self.assertEqual(_clean_venue("Uttara Club Limited"), "Uttara Club Limited")

    def test_empty_venue_stays_empty(self):
        self.assertEqual(_clean_venue(""), "")


def _fake_pdf(pages_tables: list[list[list]]):
    """pages_tables is a list of pages, each a list of tables, each a list
    of rows — mirrors pdfplumber.Page.extract_tables()'s return shape."""
    pages = []
    for tables in pages_tables:
        page = mock.Mock()
        page.extract_tables.return_value = tables
        pages.append(page)
    pdf = mock.MagicMock()
    pdf.__enter__.return_value.pages = pages
    return pdf


class ParseAgmEgmPdfTests(TestCase):
    def test_skips_title_and_header_rows_keeps_data(self):
        table = [
            ["Dhaka Stock Exchange PLC.\nCompany's AGM/EGM and Record Date Information", None, None, None, None, None, None],
            ["Last Updated 16-Aug-26", None, None, None, None, None, None],
            ["Name of the Company", "Year end", "Dividend/EGM\nPurpose", "Date of AGM/EGM", "Record\nDate", "Venue/Mode", "Time"],
            ["Al-Haj Textile Mills Limited", "June 30, 2025", "3% Cash Dividend", "14-Sep-26", "16-Aug-26", "Hybrid Platform.", "11.00 AM"],
        ]
        with mock.patch("pdfplumber.open", return_value=_fake_pdf([[table]])):
            rows = parse_agm_egm_pdf(b"fake")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["company"], "Al-Haj Textile Mills Limited")
        self.assertEqual(row["agm_egm_date"], date(2026, 9, 14))
        self.assertEqual(row["record_date"], date(2026, 8, 16))

    def test_placeholder_dates_become_none_not_dropped(self):
        table = [
            ["Name of the Company", "Year end", "Purpose", "Date of AGM/EGM", "Record\nDate", "Venue/Mode", "Time"],
            ["Al-Arafah Islami Bank PLC", "December 31, 2025", "No Dividend", "Postponed", "9-Jun-26", "Will be notified later.", "Will be notified later."],
        ]
        with mock.patch("pdfplumber.open", return_value=_fake_pdf([[table]])):
            rows = parse_agm_egm_pdf(b"fake")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["agm_egm_date"])
        self.assertEqual(rows[0]["record_date"], date(2026, 6, 9))

    def test_aggregates_across_pages(self):
        table_header = ["Name of the Company", "Year end", "Purpose", "Date of AGM/EGM", "Record\nDate", "Venue/Mode", "Time"]
        table1 = [table_header, ["Company A", "", "", "1-Jan-26", "", "", ""]]
        table2 = [table_header, ["Company B", "", "", "2-Jan-26", "", "", ""]]
        with mock.patch("pdfplumber.open", return_value=_fake_pdf([[table1], [table2]])):
            rows = parse_agm_egm_pdf(b"fake")
        self.assertEqual({r["company"] for r in rows}, {"Company A", "Company B"})

    def test_no_data_rows_raises(self):
        table = [["Name of the Company", "Year end", "Purpose", "Date of AGM/EGM", "Record\nDate", "Venue/Mode", "Time"]]
        with mock.patch("pdfplumber.open", return_value=_fake_pdf([[table]])):
            with self.assertRaises(AGMPageParseError):
                parse_agm_egm_pdf(b"fake")


class SyncDseAgmEgmEventsTests(TestCase):
    def _row(self, **overrides):
        base = {
            "company": "Al-Haj Textile Mills Limited",
            "year_end": "June 30, 2025",
            "purpose": "3% Cash Dividend",
            "agm_egm_date": date(2026, 9, 14),
            "record_date": date(2026, 8, 16),
            "venue": "Hybrid Platform.",
            "time": "11.00 AM",
        }
        base.update(overrides)
        return base

    def test_creates_one_event_per_real_date(self):
        with mock.patch("market.services.dse_events.fetch_dse_agm_egm_pdf", return_value=b"x"), \
                mock.patch("market.services.dse_events.parse_agm_egm_pdf", return_value=[self._row()]):
            result = sync_dse_agm_egm_events()

        self.assertTrue(result["ok"])
        self.assertEqual(result["created"], 2)  # AGM/EGM date + Record date
        self.assertEqual(MarketEvent.objects.count(), 2)
        agm = MarketEvent.objects.get(event_type=MarketEvent.EventType.AGM)
        self.assertEqual(agm.event_date, date(2026, 9, 14))
        self.assertTrue(agm.is_public)
        self.assertIn("Al-Haj Textile Mills Limited", agm.title)
        self.assertEqual(agm.venue, "Hybrid Platform.")
        self.assertEqual(agm.event_time, "11.00 AM")
        record = MarketEvent.objects.get(event_type=MarketEvent.EventType.RECORD_DATE)
        self.assertEqual(record.event_date, date(2026, 8, 16))
        self.assertEqual(record.details, "Purpose: 3% Cash Dividend")

    def test_placeholder_dates_are_skipped_not_errored(self):
        row = self._row(agm_egm_date=None, record_date=None)
        with mock.patch("market.services.dse_events.fetch_dse_agm_egm_pdf", return_value=b"x"), \
                mock.patch("market.services.dse_events.parse_agm_egm_pdf", return_value=[row]):
            result = sync_dse_agm_egm_events()

        self.assertTrue(result["ok"])
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["skipped_no_date"], 2)
        self.assertEqual(MarketEvent.objects.count(), 0)

    def test_rerun_with_same_data_is_unchanged_not_duplicated(self):
        with mock.patch("market.services.dse_events.fetch_dse_agm_egm_pdf", return_value=b"x"), \
                mock.patch("market.services.dse_events.parse_agm_egm_pdf", return_value=[self._row()]):
            sync_dse_agm_egm_events()
            result2 = sync_dse_agm_egm_events()

        self.assertEqual(result2["created"], 0)
        self.assertEqual(result2["updated"], 0)
        self.assertEqual(result2["unchanged"], 2)
        self.assertEqual(MarketEvent.objects.count(), 2)

    def test_placeholder_becoming_real_date_creates_new_event_not_duplicate_noise(self):
        with mock.patch("market.services.dse_events.fetch_dse_agm_egm_pdf", return_value=b"x"), \
                mock.patch("market.services.dse_events.parse_agm_egm_pdf", return_value=[self._row(agm_egm_date=None)]):
            result1 = sync_dse_agm_egm_events()
        self.assertEqual(result1["created"], 1)  # only record date
        self.assertEqual(MarketEvent.objects.count(), 1)

        with mock.patch("market.services.dse_events.fetch_dse_agm_egm_pdf", return_value=b"x"), \
                mock.patch("market.services.dse_events.parse_agm_egm_pdf", return_value=[self._row()]):
            result2 = sync_dse_agm_egm_events()
        self.assertEqual(result2["created"], 1)  # AGM date now announced
        self.assertEqual(result2["unchanged"], 1)  # record date row untouched
        self.assertEqual(MarketEvent.objects.count(), 2)

    def test_changed_venue_updates_existing_row_in_place(self):
        with mock.patch("market.services.dse_events.fetch_dse_agm_egm_pdf", return_value=b"x"), \
                mock.patch("market.services.dse_events.parse_agm_egm_pdf", return_value=[self._row()]):
            sync_dse_agm_egm_events()

        changed_row = self._row(venue="New Venue Hall")
        with mock.patch("market.services.dse_events.fetch_dse_agm_egm_pdf", return_value=b"x"), \
                mock.patch("market.services.dse_events.parse_agm_egm_pdf", return_value=[changed_row]):
            result = sync_dse_agm_egm_events()

        self.assertEqual(result["created"], 0)
        self.assertEqual(result["updated"], 2)
        self.assertEqual(MarketEvent.objects.count(), 2)
        agm = MarketEvent.objects.get(event_type=MarketEvent.EventType.AGM)
        self.assertEqual(agm.venue, "New Venue Hall")

    def test_two_events_same_company_same_date_stay_distinct(self):
        """Real-world case: an EGM (Articles amendment) and an AGM
        (dividend) for the same company can share the same date but must
        not collide into one row, since title embeds the differing
        purpose text."""
        egm_row = self._row(
            purpose="For the purpose of considering amendments to the Articles of Association.",
            agm_egm_date=date(2026, 9, 3),
            record_date=None,
        )
        agm_row = self._row(purpose="10% Cash Dividend", agm_egm_date=date(2026, 9, 3), record_date=None)
        with mock.patch("market.services.dse_events.fetch_dse_agm_egm_pdf", return_value=b"x"), \
                mock.patch("market.services.dse_events.parse_agm_egm_pdf", return_value=[egm_row, agm_row]):
            result = sync_dse_agm_egm_events()

        self.assertEqual(result["created"], 2)
        self.assertEqual(MarketEvent.objects.filter(event_date=date(2026, 9, 3)).count(), 2)
        self.assertEqual(MarketEvent.objects.filter(event_type=MarketEvent.EventType.EGM).count(), 1)
        self.assertEqual(MarketEvent.objects.filter(event_type=MarketEvent.EventType.AGM).count(), 1)

    def test_fetch_failure_reported_not_raised(self):
        import requests

        with mock.patch(
            "market.services.dse_events.fetch_dse_agm_egm_pdf",
            side_effect=requests.exceptions.ConnectionError("unreachable"),
        ):
            result = sync_dse_agm_egm_events()
        self.assertFalse(result["ok"])
        self.assertIn("unreachable", result["error"])
        self.assertEqual(MarketEvent.objects.count(), 0)

    def test_parse_failure_reported_not_raised(self):
        with mock.patch("market.services.dse_events.fetch_dse_agm_egm_pdf", return_value=b"x"), \
                mock.patch("market.services.dse_events.parse_agm_egm_pdf", side_effect=AGMPageParseError("layout changed")):
            result = sync_dse_agm_egm_events()
        self.assertFalse(result["ok"])
        self.assertIn("layout changed", result["error"])
