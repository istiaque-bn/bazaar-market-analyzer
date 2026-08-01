from unittest import mock

import requests
from django.test import TestCase

from market.models import Exchange, PriceHistory, Stock
from market.services import cse_fetcher, dse_fetcher


class DseSslFailureTests(TestCase):
    """SSL failures must fail safely — no automatic retry with certificate
    verification disabled (previously: _get() caught SSLError and retried
    via requests.get(..., verify=False), and _ensure_ssl_patch() globally
    monkey-patched requests.Session.request to do the same for any caller)."""

    def test_session_defaults_to_verified(self):
        session = dse_fetcher._session()
        self.assertNotEqual(session.verify, False)

    def test_get_propagates_ssl_error_without_insecure_retry(self):
        with mock.patch(
            "requests.Session.get", side_effect=requests.exceptions.SSLError("cert verify failed")
        ) as mock_get:
            with self.assertRaises(requests.exceptions.SSLError):
                dse_fetcher._get("https://www.dsebd.org/latest_share_price_scroll_l.php")
        # Called exactly once: no automatic insecure-retry second call.
        self.assertEqual(mock_get.call_count, 1)

    def test_ensure_ssl_patch_removed(self):
        self.assertFalse(hasattr(dse_fetcher, "_ensure_ssl_patch"))

    def test_live_scrape_fails_safely_on_ssl_error(self):
        with mock.patch(
            "requests.Session.get", side_effect=requests.exceptions.SSLError("cert verify failed")
        ):
            result = dse_fetcher.fetch_dse_live_via_scrape()
        self.assertIsNone(result)

    def test_bare_requests_get_never_called_with_verify_false(self):
        with mock.patch(
            "requests.Session.get", side_effect=requests.exceptions.SSLError("cert verify failed")
        ), mock.patch("requests.get") as mock_bare_get:
            dse_fetcher.fetch_dse_live_via_scrape()
        mock_bare_get.assert_not_called()


class CseSslFailureTests(TestCase):
    def setUp(self):
        self.stock = Stock.objects.create(exchange=Exchange.CSE, trading_code="TEST", company_name="Test Co")
        PriceHistory.objects.create(
            stock=self.stock, date="2026-01-02", open=10, high=11, low=9, close=10.5, volume=1000
        )

    def test_session_defaults_to_verified(self):
        session = cse_fetcher._session()
        self.assertNotEqual(session.verify, False)

    def test_bulk_history_fails_safely_on_ssl_error(self):
        from datetime import date

        with mock.patch(
            "requests.Session.get", side_effect=requests.exceptions.SSLError("cert verify failed")
        ):
            result = cse_fetcher.fetch_cse_history_bulk(date(2026, 1, 1), date(2026, 1, 31))
        self.assertIsNone(result)

    def test_ssl_failure_keeps_previously_valid_price_history(self):
        """A fetch failure must never wipe already-saved market data."""
        from datetime import date

        before = list(PriceHistory.objects.filter(stock=self.stock).values_list("id", flat=True))
        with mock.patch(
            "requests.Session.get", side_effect=requests.exceptions.SSLError("cert verify failed")
        ):
            cse_fetcher.fetch_cse_history_bulk(date(2026, 1, 1), date(2026, 1, 31))
        after = list(PriceHistory.objects.filter(stock=self.stock).values_list("id", flat=True))
        self.assertEqual(before, after)
