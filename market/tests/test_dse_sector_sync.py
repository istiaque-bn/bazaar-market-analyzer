"""
market/services/dse_sector_sync.py parses DSE's sector directory
(by_industrylisting.php + companylistbyindustry.php) and upserts
Stock.sector/company_name. Pure parsing helpers are tested against small
HTML fixtures mimicking the real pages' structure (see FIXTURE_* below,
modeled on the real markup captured 2026-08-20 — no live network calls
anywhere in this file); sync_dse_sector_classification is tested against
mocked fetch steps, exercising real DB upsert behavior — same split as
market.tests.test_dse_events and test_holiday_sync.
"""
from unittest import mock

from django.test import TestCase

from market.models import Exchange, Stock
from market.services.dse_sector_sync import (
    SectorPageParseError,
    fetch_industry_numbers,
    fetch_sector_companies,
    sync_dse_sector_classification,
)

FIXTURE_LISTING_HTML = """
<html><body>
<ul>
  <li><a href="companylistbyindustry.php?industryno=11">Bank</a></li>
  <li><a href="companylistbyindustry.php?industryno=18">Pharmaceuticals &amp; Chemicals</a></li>
  <li><a href="companylistbyindustry.php?industryno=18">Pharmaceuticals &amp; Chemicals</a></li>
</ul>
</body></html>
"""

FIXTURE_LISTING_HTML_EMPTY = "<html><body><p>Nothing useful here.</p></body></html>"

FIXTURE_SECTOR_HTML = """
<html><body>
<h2>List of Companies Selected Industry:
    Pharmaceuticals &amp; Chemicals</h2>
<table class="table-borderless background-white">
  <tr>
    <td>
      <a href='displayCompany.php?name=ACI' class='ab1'>ACI</a>
      (&nbsp;Advanced Chemical Industries PLC&nbsp;)<br/>
      <a href='displayCompany.php?name=ACIFORMULA' class='ab1'>ACIFORMULA</a>
      (&nbsp;ACI Formulations PLC&nbsp;)<br/>
    </td>
  </tr>
</table>
</body></html>
"""

FIXTURE_SECTOR_HTML_NO_HEADING = """
<html><body>
<table class="table-borderless background-white">
  <tr><td><a href='displayCompany.php?name=ACI' class='ab1'>ACI</a>(&nbsp;Advanced Chemical Industries PLC&nbsp;)</td></tr>
</table>
</body></html>
"""

FIXTURE_SECTOR_HTML_NO_TABLE = """
<html><body><h2>List of Companies Selected Industry: Bank</h2></body></html>
"""

FIXTURE_SECTOR_HTML_EMPTY_TABLE = """
<html><body>
<h2>List of Companies Selected Industry: Bank</h2>
<table class="table-borderless background-white"><tr><td>No members</td></tr></table>
</body></html>
"""


class FetchIndustryNumbersTests(TestCase):
    def test_parses_unique_industry_numbers_in_order(self):
        numbers = fetch_industry_numbers(html=FIXTURE_LISTING_HTML)
        self.assertEqual(numbers, ["11", "18"])

    def test_no_links_raises(self):
        with self.assertRaises(SectorPageParseError):
            fetch_industry_numbers(html=FIXTURE_LISTING_HTML_EMPTY)


class FetchSectorCompaniesTests(TestCase):
    def test_parses_sector_label_and_rows(self):
        label, rows = fetch_sector_companies("18", html=FIXTURE_SECTOR_HTML)
        self.assertEqual(label, "Pharmaceuticals & Chemicals")
        self.assertEqual(rows, [("ACI", "Advanced Chemical Industries PLC"), ("ACIFORMULA", "ACI Formulations PLC")])

    def test_missing_heading_raises(self):
        with self.assertRaises(SectorPageParseError):
            fetch_sector_companies("18", html=FIXTURE_SECTOR_HTML_NO_HEADING)

    def test_missing_table_raises(self):
        with self.assertRaises(SectorPageParseError):
            fetch_sector_companies("11", html=FIXTURE_SECTOR_HTML_NO_TABLE)

    def test_table_with_no_company_links_raises(self):
        with self.assertRaises(SectorPageParseError):
            fetch_sector_companies("11", html=FIXTURE_SECTOR_HTML_EMPTY_TABLE)


class SyncDseSectorClassificationTests(TestCase):
    def setUp(self):
        self.aci = Stock.objects.create(exchange=Exchange.DSE, trading_code="ACI", is_active=True)
        self.other = Stock.objects.create(exchange=Exchange.DSE, trading_code="OTHERCO", is_active=True)

    def test_updates_matched_stocks_and_counts_unmatched(self):
        with mock.patch("market.services.dse_sector_sync.fetch_industry_numbers", return_value=["18"]), mock.patch(
            "market.services.dse_sector_sync.fetch_sector_companies",
            return_value=("Pharmaceuticals & Chemicals", [("ACI", "Advanced Chemical Industries PLC"), ("NOPE", "Ghost Co")]),
        ):
            result = sync_dse_sector_classification()

        self.assertTrue(result["ok"])
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["unmatched"], 1)
        self.assertIn("NOPE", result["unmatched_codes_sample"])
        self.aci.refresh_from_db()
        self.assertEqual(self.aci.sector, "Pharmaceuticals & Chemicals")
        self.assertEqual(self.aci.company_name, "Advanced Chemical Industries PLC")
        # Stock rows are only ever updated, never created.
        self.assertEqual(Stock.objects.count(), 2)

    def test_rerun_with_same_data_is_unchanged_not_reupdated(self):
        with mock.patch("market.services.dse_sector_sync.fetch_industry_numbers", return_value=["18"]), mock.patch(
            "market.services.dse_sector_sync.fetch_sector_companies",
            return_value=("Pharmaceuticals & Chemicals", [("ACI", "Advanced Chemical Industries PLC")]),
        ):
            sync_dse_sector_classification()
            result2 = sync_dse_sector_classification()

        self.assertEqual(result2["updated"], 0)
        self.assertEqual(result2["unchanged"], 1)

    def test_blank_scraped_name_never_clobbers_existing_company_name(self):
        self.aci.company_name = "Manually Curated Name"
        self.aci.save(update_fields=["company_name"])
        with mock.patch("market.services.dse_sector_sync.fetch_industry_numbers", return_value=["18"]), mock.patch(
            "market.services.dse_sector_sync.fetch_sector_companies",
            return_value=("Pharmaceuticals & Chemicals", [("ACI", "")]),
        ):
            sync_dse_sector_classification()
        self.aci.refresh_from_db()
        self.assertEqual(self.aci.company_name, "Manually Curated Name")
        self.assertEqual(self.aci.sector, "Pharmaceuticals & Chemicals")

    def test_one_sector_page_failure_does_not_abort_the_rest(self):
        def fake_fetch(industryno, session=None):
            if industryno == "11":
                raise SectorPageParseError("layout changed")
            return "Pharmaceuticals & Chemicals", [("ACI", "Advanced Chemical Industries PLC")]

        with mock.patch("market.services.dse_sector_sync.fetch_industry_numbers", return_value=["11", "18"]), mock.patch(
            "market.services.dse_sector_sync.fetch_sector_companies", side_effect=fake_fetch
        ), mock.patch("market.services.dse_sector_sync.time.sleep"):
            result = sync_dse_sector_classification()

        self.assertTrue(result["ok"])
        self.assertEqual(result["sectors_failed"], 1)
        self.assertEqual(result["sectors_synced"], 1)
        self.assertEqual(result["updated"], 1)

    def test_discovery_failure_reported_not_raised(self):
        with mock.patch(
            "market.services.dse_sector_sync.fetch_industry_numbers", side_effect=SectorPageParseError("no links found")
        ):
            result = sync_dse_sector_classification()
        self.assertFalse(result["ok"])
        self.assertIn("no links found", result["error"])
        self.assertEqual(Stock.objects.filter(sector="").count(), 2)
