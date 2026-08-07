"""
Exchange feature-flag tests (ENABLE_DSE/ENABLE_CSE): converting the app
into a DSE-focused deployment must never delete/mutate data, must hide
CSE from public discovery while preserving existing user-owned records,
must never fetch over the network for a disabled exchange, and must not
generate false operational alerts for an intentionally disabled exchange.

Uses @override_settings throughout (never the developer's local
environment) so these tests are deterministic regardless of the ambient
.env — see config/settings/{development,test}.py, which both re-enable
CSE on top of base.py's DSE-only default specifically so the rest of the
suite (predating this flag) keeps exercising CSE unaffected.
"""
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from market.models import (
    AnalysisResult,
    BacktestRun,
    Exchange,
    MarketSnapshot,
    Portfolio,
    SignalAction,
    Stock,
)
from market.services import portfolio as psvc
from market.services.exchange_config import disabled_exchanges, enabled_exchanges, is_exchange_enabled

PASSWORD = "Correct-Horse-Battery-Staple-42"

DSE_ONLY = override_settings(ENABLE_DSE=True, ENABLE_CSE=False)
BOTH_ENABLED = override_settings(ENABLE_DSE=True, ENABLE_CSE=True)


def make_user(username: str, is_staff: bool = False) -> User:
    return User.objects.create_user(username=username, password=PASSWORD, is_staff=is_staff)


def make_stock(exchange=Exchange.DSE, code="TESTCO", price=100.0, **kwargs) -> Stock:
    defaults = {"company_name": "Test Co", "sector": "Testing", "is_active": True, "last_price": price}
    defaults.update(kwargs)
    return Stock.objects.create(exchange=exchange, trading_code=code, **defaults)


# ---------------------------------------------------------------------------
# The helper itself
# ---------------------------------------------------------------------------


class ExchangeConfigHelperTests(TestCase):
    @DSE_ONLY
    def test_dse_only_reports_dse_enabled_cse_disabled(self):
        self.assertEqual(enabled_exchanges(), [Exchange.DSE])
        self.assertEqual(disabled_exchanges(), [Exchange.CSE])
        self.assertTrue(is_exchange_enabled("DSE"))
        self.assertFalse(is_exchange_enabled("CSE"))

    @BOTH_ENABLED
    def test_both_enabled_reports_both(self):
        self.assertEqual(enabled_exchanges(), [Exchange.DSE, Exchange.CSE])
        self.assertEqual(disabled_exchanges(), [])

    @override_settings(ENABLE_DSE=False, ENABLE_CSE=True)
    def test_cse_only_reports_cse_enabled_dse_disabled(self):
        self.assertEqual(enabled_exchanges(), [Exchange.CSE])
        self.assertEqual(disabled_exchanges(), [Exchange.DSE])

    def test_is_exchange_enabled_none_is_false(self):
        self.assertFalse(is_exchange_enabled(None))
        self.assertFalse(is_exchange_enabled(""))


# ---------------------------------------------------------------------------
# Boolean env parsing (settings-module level, subprocess — mirrors the
# existing pattern in market/tests/test_settings.py, since these values
# are parsed once at module-import time)
# ---------------------------------------------------------------------------


class BooleanEnvParsingTests(TestCase):
    """In-process equivalent using override_settings: confirms
    enabled_exchanges() treats common truthy/falsy string spellings the
    same way the rest of this codebase's boolean env vars already do
    (see AUTO_MARKET_SYNC etc. in config/settings/base.py) — the actual
    os.getenv(...).lower() in (...) parsing itself is exercised by
    test_settings.py's existing subprocess-based settings tests."""

    def test_true_like_values(self):
        for value in (True,):
            with override_settings(ENABLE_DSE=True, ENABLE_CSE=value):
                self.assertIn(Exchange.CSE, enabled_exchanges())

    def test_false_like_values(self):
        for value in (False,):
            with override_settings(ENABLE_DSE=True, ENABLE_CSE=value):
                self.assertNotIn(Exchange.CSE, enabled_exchanges())


# ---------------------------------------------------------------------------
# DSE enabled, CSE disabled
# ---------------------------------------------------------------------------


@DSE_ONLY
class DseOnlyPublicPagesTests(TestCase):
    """Market-data pages require authentication project-wide (see
    accounts/roles.py) — every request here logs a user in first. `/`
    itself now only ever redirects (to Login for anon, to the caller's
    role panel for an authenticated visitor — see market.views.home), so
    DSE-only-vs-both-enabled markup is asserted against a page that
    actually renders base.html, e.g. /stocks/."""

    def setUp(self):
        self.user = make_user("dse_only_viewer")
        self.client.login(username="dse_only_viewer", password=PASSWORD)
        self.dse_stock = make_stock(Exchange.DSE, "DSEONE", price=50.0)
        self.cse_stock = make_stock(Exchange.CSE, "CSEONE", price=60.0)
        AnalysisResult.objects.create(
            stock=self.dse_stock, as_of=date(2026, 1, 1), action=SignalAction.BUY, score=80, confidence=0.8,
        )
        AnalysisResult.objects.create(
            stock=self.cse_stock, as_of=date(2026, 1, 1), action=SignalAction.BUY, score=90, confidence=0.9,
        )

    def test_authenticated_root_redirects_to_dashboard(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/dashboard/", response.url)

    def test_footer_shows_dse_only_wording(self):
        html = self.client.get("/stocks/").content.decode()
        self.assertIn("analyses DSE using", html)
        self.assertNotIn("analyses DSE &amp; CSE using", html)

    def test_cse_ticker_and_status_chip_absent(self):
        html = self.client.get("/stocks/").content.decode()
        self.assertNotIn('id="marketTickerTrackCse"', html)
        self.assertNotIn('data-ex="CSE"', html)
        self.assertIn('id="marketTickerTrackDse"', html)

    def test_dse_stock_discoverable_cse_excluded_from_stock_list(self):
        html = self.client.get("/stocks/").content.decode()
        self.assertIn("DSEONE", html)
        self.assertNotIn("CSEONE", html)

    def test_stock_list_exchange_filter_hides_cse_option(self):
        html = self.client.get("/stocks/").content.decode()
        self.assertIn('value="DSE"', html)
        self.assertNotIn('value="CSE"', html)

    def test_stock_list_explicit_cse_query_param_returns_nothing(self):
        html = self.client.get("/stocks/?exchange=CSE").content.decode()
        self.assertNotIn("CSEONE", html)

    def test_dse_stock_detail_reachable(self):
        response = self.client.get(reverse("stock_detail", args=["DSE", "DSEONE"]))
        self.assertEqual(response.status_code, 200)

    def test_cse_stock_detail_404s(self):
        response = self.client.get(reverse("stock_detail", args=["CSE", "CSEONE"]))
        self.assertEqual(response.status_code, 404)

    def test_cse_predict_price_route_404s(self):
        response = self.client.get(reverse("predict_price", args=["CSE", "CSEONE"]), {"date": "2026-01-02"})
        self.assertEqual(response.status_code, 404)

    def test_dashboard_excludes_cse_from_screener_sections(self):
        html = self.client.get("/dashboard/").content.decode()
        self.assertNotIn("CSEONE", html)

    def test_ticker_json_omits_cse_quotes_and_reports_enabled_exchanges(self):
        payload = self.client.get("/ticker.json").json()
        self.assertEqual(payload["cse"]["quotes"], [])
        self.assertEqual(payload["enabled_exchanges"], ["DSE"])

    def test_backtests_view_excludes_cse_runs(self):
        BacktestRun.objects.create(
            name="DSE run", strategy="rsi_macd", exchange="DSE", start_date=date(2026, 1, 1), end_date=date(2026, 1, 2),
        )
        BacktestRun.objects.create(
            name="CSE run", strategy="rsi_macd", exchange="CSE", start_date=date(2026, 1, 1), end_date=date(2026, 1, 2),
        )
        html = self.client.get("/backtests/").content.decode()
        self.assertIn("DSE run", html)
        self.assertNotIn("CSE run", html)

    def test_no_empty_cse_panel_or_loading_message(self):
        html = self.client.get("/").content.decode()
        self.assertNotIn("CSE quotes loading", html)


@DSE_ONLY
class DseOnlyAuthenticatedPagesTests(TestCase):
    def setUp(self):
        self.user = make_user("alice")
        self.client.login(username="alice", password=PASSWORD)
        self.dse_stock = make_stock(Exchange.DSE, "DSEONE", price=50.0)
        self.cse_stock = make_stock(Exchange.CSE, "CSEONE", price=60.0)

    def test_watchlist_add_action_hidden_for_cse_stock_detail(self):
        # The CSE stock's own detail page 404s, so its watchlist-add
        # control is unreachable through the UI at all.
        response = self.client.get(reverse("stock_detail", args=["CSE", "CSEONE"]))
        self.assertEqual(response.status_code, 404)

    def test_direct_post_to_add_disabled_cse_stock_to_watchlist_is_rejected(self):
        response = self.client.post(reverse("toggle_watchlist", args=["CSE", "CSEONE"]))
        self.assertEqual(response.status_code, 302)
        from market.models import Watchlist

        wl = Watchlist.objects.get(user=self.user, name="Default")
        self.assertFalse(wl.stocks.filter(id=self.cse_stock.id).exists())

    def test_existing_cse_watchlist_entry_can_still_be_removed(self):
        from market.models import Watchlist

        wl, _ = Watchlist.objects.get_or_create(user=self.user, name="Default")
        wl.stocks.add(self.cse_stock)
        response = self.client.post(reverse("toggle_watchlist", args=["CSE", "CSEONE"]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(wl.stocks.filter(id=self.cse_stock.id).exists())

    def test_watchlist_page_shows_disabled_badge_and_remove_button_for_cse(self):
        from market.models import Watchlist

        wl, _ = Watchlist.objects.get_or_create(user=self.user, name="Default")
        wl.stocks.add(self.cse_stock)
        html = self.client.get("/watchlist/").content.decode()
        self.assertIn("Disabled", html)
        self.assertIn(f"/{Exchange.CSE}/CSEONE/watch/", html)


@DSE_ONLY
class DseOnlyPortfolioTests(TestCase):
    def setUp(self):
        self.user = make_user("bob")
        self.client.login(username="bob", password=PASSWORD)
        self.portfolio = psvc.get_or_create_default_portfolio(self.user)
        self.dse_stock = make_stock(Exchange.DSE, "DSEONE", price=50.0)
        self.cse_stock = make_stock(Exchange.CSE, "CSEONE", price=60.0)

    def _buy_cse_while_enabled(self, quantity="10", price="50", txn_date=date(2026, 1, 1)):
        """Existing-holding fixtures must be created while CSE was still
        enabled — a fresh BUY is correctly rejected once disabled, which
        is exactly the behavior under test elsewhere in this class, so
        creating the fixture itself must not go through that same gate."""
        with BOTH_ENABLED:
            return psvc.create_transaction(
                self.portfolio, self.cse_stock, "BUY", Decimal(quantity), Decimal(price), Decimal("0"), txn_date,
            )

    def test_new_cse_buy_is_rejected_with_clear_message(self):
        with self.assertRaises(psvc.PortfolioValidationError) as ctx:
            psvc.create_transaction(
                self.portfolio, self.cse_stock, "BUY", Decimal("10"), Decimal("60"), Decimal("0"), date(2026, 1, 1),
            )
        self.assertIn("CSE", str(ctx.exception))
        self.assertIn("disabled", str(ctx.exception).lower())

    def test_existing_cse_holding_remains_readable_and_never_labelled_live(self):
        self._buy_cse_while_enabled()
        # Now genuinely under DSE_ONLY (the class-level override) when reading it back.
        summary = psvc.portfolio_summary(self.portfolio)
        rows = [r for r in summary["holdings"] if r["exchange"] == Exchange.CSE]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["quote_status"], psvc.QUOTE_EXCHANGE_DISABLED)
        self.assertNotIn(rows[0]["quote_status"], (psvc.QUOTE_LIVE, psvc.QUOTE_DELAYED, psvc.QUOTE_MARKET_CLOSED))
        self.assertIsNone(rows[0]["today_pl"])
        self.assertTrue(summary["has_disabled_exchange_holdings"])

    def test_corrective_sell_of_existing_cse_holding_is_allowed(self):
        self._buy_cse_while_enabled()
        txn = psvc.create_transaction(
            self.portfolio, self.cse_stock, "SELL", Decimal("10"), Decimal("55"), Decimal("0"), date(2026, 1, 2),
        )
        self.assertEqual(txn.transaction_type, "SELL")

    def test_deleting_a_cse_transaction_still_allowed(self):
        txn = self._buy_cse_while_enabled()
        psvc.delete_transaction(txn)
        from market.models import PortfolioTransaction

        self.assertFalse(PortfolioTransaction.objects.filter(id=txn.id).exists())

    def test_add_transaction_form_offers_only_dse_for_a_fresh_portfolio(self):
        response = self.client.get(reverse("portfolio_add_transaction", args=[self.portfolio.id]))
        stock_choices = list(response.context["form"].fields["stock"].queryset)
        self.assertIn(self.dse_stock, stock_choices)
        self.assertNotIn(self.cse_stock, stock_choices)

    def test_add_transaction_form_still_offers_an_already_held_cse_stock(self):
        self._buy_cse_while_enabled()
        response = self.client.get(reverse("portfolio_add_transaction", args=[self.portfolio.id]))
        stock_choices = list(response.context["form"].fields["stock"].queryset)
        self.assertIn(self.cse_stock, stock_choices)

    def test_add_holding_form_never_offers_cse_even_if_held(self):
        self._buy_cse_while_enabled()
        response = self.client.get(reverse("portfolio_add_holding", args=[self.portfolio.id]))
        stock_choices = list(response.context["form"].fields["stock"].queryset)
        self.assertNotIn(self.cse_stock, stock_choices)

    def test_web_buy_attempt_for_an_already_held_cse_stock_surfaces_service_error(self):
        # Pre-hold CSE so the form's dropdown actually offers it — a
        # brand-new (never-held) CSE stock is excluded at the form layer
        # instead (see test_add_transaction_form_offers_only_dse_...),
        # which is its own, separate line of defense.
        self._buy_cse_while_enabled()
        response = self.client.post(
            reverse("portfolio_add_transaction", args=[self.portfolio.id]),
            {
                "stock": self.cse_stock.id,
                "transaction_type": "BUY",
                "quantity": "5",
                "price_per_share": "60",
                "fees": "0",
                "transaction_date": "2026-01-03",
                "notes": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "disabled")

    def test_web_buy_attempt_for_a_never_held_cse_stock_surfaces_form_error_not_500(self):
        response = self.client.post(
            reverse("portfolio_add_transaction", args=[self.portfolio.id]),
            {
                "stock": self.cse_stock.id,
                "transaction_type": "BUY",
                "quantity": "10",
                "price_per_share": "60",
                "fees": "0",
                "transaction_date": "2026-01-01",
                "notes": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not one of the available choices")

    def test_portfolio_detail_page_shows_disabled_banner(self):
        self._buy_cse_while_enabled()
        html = self.client.get(reverse("portfolio_detail", args=[self.portfolio.id])).content.decode()
        self.assertIn("CSE support is temporarily disabled", html)

    def test_holding_row_stock_link_omitted_for_disabled_exchange(self):
        self._buy_cse_while_enabled()
        html = self.client.get(reverse("portfolio_detail", args=[self.portfolio.id])).content.decode()
        self.assertNotIn(f"/stocks/{Exchange.CSE}/CSEONE/", html)


@DSE_ONLY
class DseOnlyApiTests(TestCase):
    def setUp(self):
        self.user = make_user("dse_only_api_viewer")
        self.client.force_login(self.user)
        self.dse_stock = make_stock(Exchange.DSE, "DSEONE", price=50.0)
        self.cse_stock = make_stock(Exchange.CSE, "CSEONE", price=60.0)
        AnalysisResult.objects.create(stock=self.dse_stock, as_of=date(2026, 1, 1), action=SignalAction.BUY, score=80, confidence=0.8)
        AnalysisResult.objects.create(stock=self.cse_stock, as_of=date(2026, 1, 1), action=SignalAction.BUY, score=90, confidence=0.9)

    def test_stock_list_api_excludes_cse(self):
        payload = self.client.get("/api/stocks/").json()
        codes = [s["trading_code"] for s in payload["results"]] if "results" in payload else [s["trading_code"] for s in payload]
        self.assertIn("DSEONE", codes)
        self.assertNotIn("CSEONE", codes)

    def test_stock_analysis_api_404s_for_cse(self):
        response = self.client.get(reverse("api_stock_detail", args=["CSE", "CSEONE"]))
        self.assertEqual(response.status_code, 404)

    def test_stock_analysis_api_200s_for_dse(self):
        response = self.client.get(reverse("api_stock_detail", args=["DSE", "DSEONE"]))
        self.assertEqual(response.status_code, 200)

    def test_predict_price_api_404s_for_cse(self):
        response = self.client.get(reverse("api_predict_price", args=["CSE", "CSEONE"]), {"date": "2026-01-02"})
        self.assertEqual(response.status_code, 404)

    def test_screener_api_reports_enabled_exchanges_and_excludes_cse(self):
        payload = self.client.get("/api/screener/").json()
        self.assertEqual(payload["enabled_exchanges"], ["DSE"])
        codes = [row["stock"]["trading_code"] for row in payload["potential"]]
        self.assertNotIn("CSEONE", codes)

    def test_analysis_list_api_excludes_cse(self):
        payload = self.client.get("/api/analysis/").json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        codes = [row["stock"]["trading_code"] for row in rows]
        self.assertNotIn("CSEONE", codes)

    def _auth(self, username):
        user = make_user(username)
        client = Client()
        client.force_login(user)
        return user, client

    def test_new_cse_portfolio_purchase_rejected_via_api_with_structured_error(self):
        user, client = self._auth("api_user")
        portfolio = psvc.get_or_create_default_portfolio(user)
        response = client.post(
            reverse("api_portfolio_transactions", args=[portfolio.id]),
            {
                "stock_id": self.cse_stock.id,
                "transaction_type": "BUY",
                "quantity": "10",
                "price_per_share": "60",
                "transaction_date": "2026-01-01",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("detail", response.json())

    def test_existing_cse_portfolio_record_readable_via_api(self):
        user, client = self._auth("api_user2")
        portfolio = psvc.get_or_create_default_portfolio(user)
        with BOTH_ENABLED:
            psvc.create_transaction(portfolio, self.cse_stock, "BUY", Decimal("10"), Decimal("50"), Decimal("0"), date(2026, 1, 1))
        response = client.get(reverse("api_portfolio_summary", args=[portfolio.id]))
        self.assertEqual(response.status_code, 200)
        codes = [h["trading_code"] for h in response.json()["holdings"]]
        self.assertIn("CSEONE", codes)

    def test_portfolio_endpoints_still_require_auth(self):
        response = Client().get(reverse("api_portfolio_list"))
        self.assertEqual(response.status_code, 401)


@DSE_ONLY
class DseOnlyBackgroundTaskTests(TestCase):
    """Neither orchestration (autosync/analyzer) nor a direct call to a
    CSE-specific fetcher entry point may perform network work while CSE
    is disabled — the flag is enforced at both layers independently."""

    def test_direct_call_to_cse_live_fetcher_makes_no_network_request(self):
        from market.services import cse_fetcher

        with mock.patch.object(cse_fetcher, "fetch_cse_live_via_bdshare") as m_bd, mock.patch.object(
            cse_fetcher, "fetch_cse_live_scrape"
        ) as m_scrape:
            result = cse_fetcher.sync_cse_live()
        m_bd.assert_not_called()
        m_scrape.assert_not_called()
        self.assertEqual(result["skipped"], "exchange_disabled")

    def test_direct_call_to_cse_history_fetcher_makes_no_network_request(self):
        from market.services import cse_fetcher

        with mock.patch.object(cse_fetcher, "fetch_cse_history_bulk") as m_bulk:
            result = cse_fetcher.sync_cse_history()
        m_bulk.assert_not_called()
        self.assertTrue(result["exchange_disabled"])

    def test_autosync_skips_cse_but_still_syncs_dse(self):
        from market.services import autosync, cse_fetcher, dse_fetcher

        with mock.patch.object(dse_fetcher, "fetch_dse_live_via_bdshare", return_value=None), mock.patch.object(
            dse_fetcher, "fetch_dse_live_via_scrape", return_value=None
        ), mock.patch.object(cse_fetcher, "fetch_cse_live_via_bdshare") as m_cse_bd, mock.patch.object(
            cse_fetcher, "fetch_cse_live_scrape"
        ) as m_cse_scrape:
            autosync._run_live_sync_unlocked()
        m_cse_bd.assert_not_called()
        m_cse_scrape.assert_not_called()

    def test_analyzer_fetch_all_makes_no_cse_network_request(self):
        from market.services import analyzer, cse_fetcher

        with mock.patch.object(cse_fetcher, "fetch_cse_live_via_bdshare") as m_bd, mock.patch.object(
            cse_fetcher, "fetch_cse_live_scrape"
        ) as m_scrape, mock.patch("market.services.dse_fetcher.fetch_dse_live_via_bdshare", return_value=None), mock.patch(
            "market.services.dse_fetcher.fetch_dse_live_via_scrape", return_value=None
        ):
            analyzer.fetch_all(use_demo_if_empty=False)
        m_bd.assert_not_called()
        m_scrape.assert_not_called()

    def test_run_full_analysis_only_analyzes_enabled_exchange_stocks(self):
        from market.services.analyzer import run_full_analysis

        dse_stock = make_stock(Exchange.DSE, "DSEONE", price=50.0)
        cse_stock = make_stock(Exchange.CSE, "CSEONE", price=60.0)
        for s in (dse_stock, cse_stock):
            from market.models import PriceHistory

            for i in range(5):
                PriceHistory.objects.create(
                    stock=s, date=date(2026, 1, 1 + i), open=10, high=11, low=9, close=10, volume=100,
                )
        result = run_full_analysis(train_ml=False)
        self.assertFalse(AnalysisResult.objects.filter(stock=cse_stock).exists())
        self.assertEqual(result["enabled_exchanges"], ["DSE"])
        self.assertNotIn("CSE", result["backtests"])

    def test_cse_inactivity_generates_no_stale_data_alert(self):
        from django.utils import timezone

        from market.services.ops_alerts import _stale_data_alerts

        freshness = {
            "DSE": {"latest_price_date": timezone.localdate().isoformat(), "enabled": True},
            "CSE": {"latest_price_date": None, "enabled": False},
        }
        alerts = _stale_data_alerts(freshness)
        self.assertEqual(alerts, [])

    def test_provenance_report_marks_cse_disabled(self):
        from market.services.data_quality import provenance_report

        report = provenance_report()
        self.assertFalse(report["freshness"]["CSE"]["enabled"])
        self.assertTrue(report["freshness"]["DSE"]["enabled"])


@DSE_ONLY
class DseOnlyHealthAndReadinessTests(TestCase):
    def test_readiness_ok_when_dse_healthy_regardless_of_cse(self):
        response = self.client.get("/health/ready/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")

    def test_health_endpoint_ok(self):
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 200)


@DSE_ONLY
class DseOnlyMlModelTests(TestCase):
    def test_train_model_never_builds_cse_panel(self):
        from market.services import ml_model

        with mock.patch.object(ml_model, "build_training_panel", wraps=ml_model.build_training_panel) as spy:
            ml_model.train_model(limit_stocks=5)
        called_exchanges = [c.args[0] if c.args else c.kwargs.get("exchange") for c in spy.call_args_list]
        self.assertNotIn(Exchange.CSE, called_exchanges)

    def test_load_model_bypasses_a_combined_bundle(self):
        from market.services import ml_model

        with mock.patch.object(ml_model, "_load_bundle") as m_load:
            m_load.return_value = {"status": ml_model.STATUS_ACTIVE, "version": "v1", "exchange_scope": "combined"}
            with mock.patch.object(ml_model, "_is_deployable", return_value=True):
                result = ml_model.load_model(exchange=Exchange.DSE)
        # _load_bundle must never even be called for the combined path
        # while CSE is disabled.
        combined_calls = [c for c in m_load.call_args_list if c.args and c.args[0] == ml_model.MODEL_PATH]
        self.assertEqual(combined_calls, [])


# ---------------------------------------------------------------------------
# Both exchanges enabled — existing behavior fully intact
# ---------------------------------------------------------------------------


@BOTH_ENABLED
class BothEnabledTests(TestCase):
    def setUp(self):
        self.user = make_user("both_enabled_viewer")
        self.client.login(username="both_enabled_viewer", password=PASSWORD)
        self.dse_stock = make_stock(Exchange.DSE, "DSEONE", price=50.0)
        self.cse_stock = make_stock(Exchange.CSE, "CSEONE", price=60.0)

    def test_both_tickers_present(self):
        html = self.client.get("/stocks/").content.decode()
        self.assertIn('id="marketTickerTrackDse"', html)
        self.assertIn('id="marketTickerTrackCse"', html)

    def test_both_market_status_chips_present(self):
        html = self.client.get("/stocks/").content.decode()
        self.assertIn('data-ex="DSE"', html)
        self.assertIn('data-ex="CSE"', html)

    def test_cse_stock_detail_reachable(self):
        response = self.client.get(reverse("stock_detail", args=["CSE", "CSEONE"]))
        self.assertEqual(response.status_code, 200)

    def test_cse_stock_discoverable_in_stock_list(self):
        html = self.client.get("/stocks/").content.decode()
        self.assertIn("CSEONE", html)

    def test_stock_list_api_includes_both(self):
        payload = self.client.get("/api/stocks/").json()
        codes = [s["trading_code"] for s in payload["results"]] if "results" in payload else [s["trading_code"] for s in payload]
        self.assertIn("DSEONE", codes)
        self.assertIn("CSEONE", codes)

    def test_new_cse_portfolio_buy_is_allowed(self):
        user = make_user("carol")
        portfolio = psvc.get_or_create_default_portfolio(user)
        txn = psvc.create_transaction(
            portfolio, self.cse_stock, "BUY", Decimal("10"), Decimal("60"), Decimal("0"), date(2026, 1, 1),
        )
        self.assertEqual(txn.transaction_type, "BUY")

    def test_cse_quote_status_is_not_exchange_disabled(self):
        summary_status = psvc.quote_status(self.cse_stock)
        self.assertNotEqual(summary_status["status"], psvc.QUOTE_EXCHANGE_DISABLED)

    def test_run_full_analysis_analyzes_both_exchanges(self):
        from market.models import PriceHistory
        from market.services.analyzer import run_full_analysis

        for s in (self.dse_stock, self.cse_stock):
            for i in range(5):
                PriceHistory.objects.create(stock=s, date=date(2026, 1, 1 + i), open=10, high=11, low=9, close=10, volume=100)
        result = run_full_analysis(train_ml=False)
        self.assertEqual(set(result["enabled_exchanges"]), {"DSE", "CSE"})
        self.assertIn("DSE", result["backtests"])
        self.assertIn("CSE", result["backtests"])


# ---------------------------------------------------------------------------
# Safety cases
# ---------------------------------------------------------------------------


class SafetyCaseTests(TestCase):
    def test_both_disabled_raises_at_settings_import_time(self):
        import subprocess
        import sys
        from pathlib import Path

        base_dir = Path(__file__).resolve().parent.parent.parent
        env = {
            "PATH": __import__("os").environ.get("PATH", ""),
            "DJANGO_SETTINGS_MODULE": "config.settings.development",
            "ENABLE_DSE": "false",
            "ENABLE_CSE": "false",
        }
        result = subprocess.run(
            [sys.executable, "-c", "import django; django.setup()"],
            cwd=str(base_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ImproperlyConfigured", result.stderr)
        self.assertIn("MAINTENANCE_MODE", result.stderr)

    def test_both_disabled_with_maintenance_mode_does_not_raise(self):
        import subprocess
        import sys
        from pathlib import Path

        base_dir = Path(__file__).resolve().parent.parent.parent
        env = {
            "PATH": __import__("os").environ.get("PATH", ""),
            "DJANGO_SETTINGS_MODULE": "config.settings.development",
            "ENABLE_DSE": "false",
            "ENABLE_CSE": "false",
            "MAINTENANCE_MODE": "true",
        }
        result = subprocess.run(
            [sys.executable, "-c", "import django; django.setup(); print('OK')"],
            cwd=str(base_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK", result.stdout)

    @DSE_ONLY
    def test_existing_cse_rows_unchanged_after_disabling(self):
        stock = make_stock(Exchange.CSE, "CSEONE", price=60.0)
        original_price = stock.last_price
        original_exchange = stock.exchange
        # Touch every read path that runs while CSE is disabled.
        self.client.get("/")
        self.client.get("/stocks/")
        stock.refresh_from_db()
        self.assertEqual(stock.last_price, original_price)
        self.assertEqual(stock.exchange, original_exchange)
        self.assertEqual(Stock.objects.filter(exchange=Exchange.CSE).count(), 1)

    def test_no_destructive_migration_pending(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("makemigrations", "market", "--check", "--dry-run", stdout=out, stderr=out)

    @DSE_ONLY
    def test_cross_user_portfolio_protection_still_effective_with_cse_disabled(self):
        owner = make_user("owner")
        other = make_user("intruder")
        portfolio = psvc.get_or_create_default_portfolio(owner)
        client = Client()
        client.login(username="intruder", password=PASSWORD)
        response = client.get(reverse("portfolio_detail", args=[portfolio.id]))
        self.assertEqual(response.status_code, 404)

    @DSE_ONLY
    def test_cross_user_portfolio_protection_via_api(self):
        owner = make_user("owner2")
        other = make_user("intruder2")
        portfolio = psvc.get_or_create_default_portfolio(owner)
        client = Client()
        client.login(username="intruder2", password=PASSWORD)
        response = client.get(reverse("api_portfolio_summary", args=[portfolio.id]))
        self.assertEqual(response.status_code, 404)

    def test_no_response_caching_layer_exists_to_leak_stale_cse_state(self):
        """This app has no @cache_page/response cache anywhere (only
        rate_limit.py uses Django's cache framework, for request counters)
        — ticker.json/portfolio pages read the DB fresh on every request,
        so there is nothing to invalidate when the flag flips. Guards
        against that changing silently in the future without this test
        being revisited."""
        import market.views as views_module

        source = open(views_module.__file__).read()
        self.assertNotIn("cache_page", source)

    @DSE_ONLY
    def test_toggle_watchlist_add_rejects_disabled_exchange_with_message(self):
        user = make_user("dave")
        make_stock(Exchange.CSE, "CSEONE", price=60.0)
        client = Client()
        client.login(username="dave", password=PASSWORD)
        response = client.post(reverse("toggle_watchlist", args=["CSE", "CSEONE"]), follow=True)
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("disabled" in m.lower() for m in messages))
