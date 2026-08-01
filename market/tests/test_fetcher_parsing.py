"""Fetcher parsing tests using saved representative fixtures + mocked HTTP.

No live network access — every _get/_post call is patched to return canned
responses built from market/tests/fixtures/*, so these are deterministic
and offline.
"""
from datetime import date
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from market.services import cse_fetcher, dse_fetcher

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _resp(text=None, content=None, status_code=200, headers=None):
    resp = mock.Mock()
    resp.status_code = status_code
    if text is not None:
        resp.text = text
    if content is not None:
        resp.content = content
    resp.headers = headers or {}
    resp.raise_for_status = mock.Mock()
    return resp


class DseLiveScrapeParsingTests(SimpleTestCase):
    def test_parses_representative_scroll_table(self):
        html = _read("dse_latest_sample.html")
        with mock.patch("market.services.dse_fetcher._get", return_value=_resp(text=html)):
            df = dse_fetcher.fetch_dse_live_via_scrape()
        self.assertIsNotNone(df)
        self.assertEqual(len(df), 3)
        codes = set(df["trading_code"])
        self.assertEqual(codes, {"GP", "SQURPHARMA", "BEXIMCO"})
        gp = df[df["trading_code"] == "GP"].iloc[0]
        self.assertEqual(gp["ltp"], 285.30)
        self.assertEqual(gp["high"], 288.00)
        self.assertEqual(gp["low"], 282.10)
        self.assertEqual(gp["volume"], 53210)

    def test_no_table_found_returns_none(self):
        with mock.patch("market.services.dse_fetcher._get", return_value=_resp(text="<html><body>no data</body></html>")):
            df = dse_fetcher.fetch_dse_live_via_scrape()
        self.assertIsNone(df)

    def test_http_error_status_returns_none_not_raises(self):
        resp = _resp(text="")
        resp.raise_for_status.side_effect = Exception("500 server error")
        with mock.patch("market.services.dse_fetcher._get", return_value=resp):
            df = dse_fetcher.fetch_dse_live_via_scrape()
        self.assertIsNone(df)


class DseArchiveParsingTests(SimpleTestCase):
    def test_parses_representative_archive_table(self):
        html = _read("dse_archive_sample.html")
        with mock.patch("market.services.dse_fetcher._get", return_value=_resp(status_code=200, text=html)):
            df = dse_fetcher.fetch_dse_history_via_archive("GP", date(2026, 1, 1), date(2026, 2, 28))
        self.assertIsNotNone(df)
        self.assertEqual(len(df), 3)
        self.assertTrue(df["date"].is_monotonic_increasing)
        newest = df.iloc[-1]
        self.assertEqual(newest["date"], date(2026, 2, 2))
        self.assertEqual(newest["close"], 285.30)

    def test_date_range_filters_out_rows_outside_window(self):
        html = _read("dse_archive_sample.html")
        # Excludes the 15-Jan-2026 row (start is after it).
        with mock.patch("market.services.dse_fetcher._get", return_value=_resp(status_code=200, text=html)):
            df = dse_fetcher.fetch_dse_history_via_archive("GP", date(2026, 1, 20), date(2026, 2, 28))
        self.assertEqual(len(df), 2)
        self.assertTrue((df["date"] >= date(2026, 1, 20)).all())

    def test_no_day_end_marker_returns_none(self):
        html = """
        <table class="shares-table">
          <tr><th>DATE</th><th>OPENP*</th><th>HIGH</th><th>LOW</th><th>CLOSEP*</th></tr>
          <tr><td>No day end record found for the selected date range</td><td></td><td></td><td></td><td></td></tr>
        </table>
        """
        with mock.patch("market.services.dse_fetcher._get", return_value=_resp(status_code=200, text=html)):
            df = dse_fetcher.fetch_dse_history_via_archive("GP", date(2026, 1, 1), date(2026, 2, 28))
        self.assertIsNone(df)

    def test_non_200_status_returns_none(self):
        with mock.patch("market.services.dse_fetcher._get", return_value=_resp(status_code=503, text="")):
            df = dse_fetcher.fetch_dse_history_via_archive("GP", date(2026, 1, 1), date(2026, 2, 28))
        self.assertIsNone(df)


class CseLiveScrapeParsingTests(SimpleTestCase):
    def test_parses_representative_current_price_table(self):
        html = _read("cse_current_price_sample.html")
        with mock.patch("market.services.cse_fetcher._get", return_value=_resp(text=html)):
            df = cse_fetcher.fetch_cse_live_scrape()
        self.assertIsNotNone(df)
        self.assertEqual(len(df), 2)
        row = df[df["trading_code"] == "ABCBANK"].iloc[0]
        self.assertEqual(row["ltp"], 45.50)
        self.assertEqual(row["high"], 46.00)
        self.assertEqual(row["low"], 44.80)
        self.assertEqual(row["ycp"], 44.30)
        self.assertEqual(row["volume"], 125000)

    def test_falls_through_candidate_urls_when_first_has_no_table(self):
        html = _read("cse_current_price_sample.html")
        empty = _resp(text="<html><body>nothing here</body></html>")
        good = _resp(text=html)
        with mock.patch("market.services.cse_fetcher._get", side_effect=[empty, good]):
            df = cse_fetcher.fetch_cse_live_scrape()
        self.assertIsNotNone(df)
        self.assertEqual(len(df), 2)


class CseBulkHistoryParsingTests(SimpleTestCase):
    def _mock_csrf_response(self):
        html = '<html><body><input name="csrf_cse_token" value="tok-123"></body></html>'
        return _resp(status_code=200, text=html)

    def _mock_bulk_response(self):
        content = (FIXTURES / "cse_bulk_sample.xlsx").read_bytes()
        return _resp(
            status_code=200,
            content=content,
            headers={"content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
        )

    def test_parses_representative_bulk_excel_export(self):
        with mock.patch("market.services.cse_fetcher._get", return_value=self._mock_csrf_response()), mock.patch(
            "market.services.cse_fetcher._post", return_value=self._mock_bulk_response()
        ):
            df = cse_fetcher.fetch_cse_history_bulk(date(2026, 1, 1), date(2026, 2, 28))
        self.assertIsNotNone(df)
        self.assertEqual(len(df), 3)
        self.assertEqual(set(df.columns), {"date", "symbol", "open", "high", "low", "close", "volume", "value"})
        abc_latest = df[(df["symbol"] == "ABCBANK") & (df["date"] == date(2026, 2, 2))].iloc[0]
        self.assertEqual(abc_latest["close"], 45.50)
        self.assertEqual(abc_latest["volume"], 125000)

    def test_date_range_filters_bulk_rows(self):
        with mock.patch("market.services.cse_fetcher._get", return_value=self._mock_csrf_response()), mock.patch(
            "market.services.cse_fetcher._post", return_value=self._mock_bulk_response()
        ):
            df = cse_fetcher.fetch_cse_history_bulk(date(2026, 2, 2), date(2026, 2, 28))
        # Only the 2026-02-02 rows (2 symbols) survive the [start,end] mask.
        self.assertEqual(len(df), 2)
        self.assertTrue((df["date"] >= date(2026, 2, 2)).all())

    def test_missing_csrf_token_returns_none(self):
        no_token_resp = _resp(status_code=200, text="<html><body>no token here</body></html>")
        with mock.patch("market.services.cse_fetcher._get", return_value=no_token_resp):
            df = cse_fetcher.fetch_cse_history_bulk(date(2026, 1, 1), date(2026, 2, 28))
        self.assertIsNone(df)

    def test_html_error_page_instead_of_excel_returns_none(self):
        html_resp = _resp(
            status_code=200,
            content=b"<html><body>" + b"x" * 1000 + b"</body></html>",
            headers={"content-type": "text/html"},
        )
        with mock.patch("market.services.cse_fetcher._get", return_value=self._mock_csrf_response()), mock.patch(
            "market.services.cse_fetcher._post", return_value=html_resp
        ):
            df = cse_fetcher.fetch_cse_history_bulk(date(2026, 1, 1), date(2026, 2, 28))
        self.assertIsNone(df)
