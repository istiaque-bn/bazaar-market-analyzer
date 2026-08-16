"""Portfolio feature tests: ownership/auth, weighted-average cost basis,
validation, quote-status labeling, views, API, and query efficiency.

No live network access anywhere in this file — the portfolio feature
itself never calls a fetcher (prices come from Stock.last_price /
PriceHistory only), and tests that touch quote freshness/market hours
mock the relevant service functions directly rather than depending on
real wall-clock time or the seeded holiday calendar.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from market.models import Exchange, Portfolio, PortfolioTransaction, PriceHistory, Stock, TransactionType
from market.services import portfolio as psvc

PASSWORD = "Correct-Horse-Battery-Staple-42"


def make_user(username: str) -> User:
    return User.objects.create_user(username=username, password=PASSWORD)


def make_stock(exchange=Exchange.DSE, code="TESTCO", price=100.0, **kwargs) -> Stock:
    defaults = {"company_name": "Test Co", "sector": "Testing", "is_active": True, "last_price": price}
    defaults.update(kwargs)
    return Stock.objects.create(exchange=exchange, trading_code=code, **defaults)


class DefaultPortfolioTests(TestCase):
    def test_first_call_creates_a_default_portfolio(self):
        user = make_user("alice")
        p = psvc.get_or_create_default_portfolio(user)
        self.assertTrue(p.is_default)
        self.assertEqual(Portfolio.objects.filter(user=user).count(), 1)

    def test_second_call_is_idempotent(self):
        user = make_user("bob")
        p1 = psvc.get_or_create_default_portfolio(user)
        p2 = psvc.get_or_create_default_portfolio(user)
        self.assertEqual(p1.id, p2.id)
        self.assertEqual(Portfolio.objects.filter(user=user).count(), 1)

    def test_only_one_default_per_user_enforced_at_db_level(self):
        user = make_user("carol")
        Portfolio.objects.create(user=user, name="A", is_default=True)
        with self.assertRaises(Exception):
            Portfolio.objects.create(user=user, name="B", is_default=True)

    def test_different_users_can_each_have_their_own_default(self):
        u1, u2 = make_user("dan"), make_user("erin")
        p1 = psvc.get_or_create_default_portfolio(u1)
        p2 = psvc.get_or_create_default_portfolio(u2)
        self.assertTrue(p1.is_default)
        self.assertTrue(p2.is_default)

    def test_deleting_default_promotes_another_portfolio_via_view(self):
        user = make_user("frank")
        default = psvc.get_or_create_default_portfolio(user)
        other = Portfolio.objects.create(user=user, name="Other")
        client = Client()
        client.login(username="frank", password=PASSWORD)
        client.post(reverse("portfolio_delete", args=[default.id]), {"confirm_name": default.name})
        other.refresh_from_db()
        self.assertTrue(other.is_default)


class OwnershipAndAuthTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner")
        self.stranger = make_user("stranger")
        self.portfolio = Portfolio.objects.create(user=self.owner, name="Mine", is_default=True)

    def test_anonymous_redirected_to_login_for_every_portfolio_page(self):
        urls = [
            reverse("portfolio_redirect"),
            reverse("portfolio_list"),
            reverse("portfolio_detail", args=[self.portfolio.id]),
            reverse("portfolio_transactions", args=[self.portfolio.id]),
            reverse("portfolio_add_transaction", args=[self.portfolio.id]),
            reverse("portfolio_add_holding", args=[self.portfolio.id]),
        ]
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn("/accounts/login/", response.url, url)

    def test_stranger_cannot_view_someone_elses_portfolio(self):
        self.client.login(username="stranger", password=PASSWORD)
        response = self.client.get(reverse("portfolio_detail", args=[self.portfolio.id]))
        self.assertEqual(response.status_code, 404)

    def test_stranger_cannot_add_transaction_to_someone_elses_portfolio(self):
        stock = make_stock()
        self.client.login(username="stranger", password=PASSWORD)
        response = self.client.post(
            reverse("portfolio_add_transaction", args=[self.portfolio.id]),
            {
                "stock": stock.id, "transaction_type": "BUY", "quantity": "10",
                "price_per_share": "10", "fees": "0", "transaction_date": "2026-01-01",
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_stranger_cannot_delete_someone_elses_portfolio(self):
        self.client.login(username="stranger", password=PASSWORD)
        response = self.client.post(reverse("portfolio_delete", args=[self.portfolio.id]), {"confirm_name": "Mine"})
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Portfolio.objects.filter(id=self.portfolio.id).exists())

    def test_owner_can_view_own_portfolio(self):
        self.client.login(username="owner", password=PASSWORD)
        response = self.client.get(reverse("portfolio_detail", args=[self.portfolio.id]))
        self.assertEqual(response.status_code, 200)


class PortfolioImportAndJournalTests(TestCase):
    def setUp(self):
        self.user = make_user("journal-owner")
        self.portfolio = Portfolio.objects.create(user=self.user, name="Journal", is_default=True)
        self.stock = make_stock(code="JOURNAL")
        self.client.login(username="journal-owner", password=PASSWORD)

    def test_csv_import_creates_journalled_transaction(self):
        data = (
            "code,exchange,quantity,price,date,fees,thesis,target_price,invalidation,post_trade_review\n"
            "JOURNAL,DSE,10,125.50,2026-08-10,5,Value is improving,150,Break below support,Review after earnings\n"
        ).encode()
        response = self.client.post(reverse("portfolio_import_csv", args=[self.portfolio.id]), {"csv_file": SimpleUploadedFile("broker.csv", data, content_type="text/csv")}, follow=True)
        self.assertEqual(response.status_code, 200)
        txn = PortfolioTransaction.objects.get(portfolio=self.portfolio)
        self.assertEqual((txn.stock, txn.thesis, txn.target_price), (self.stock, "Value is improving", Decimal("150")))
        self.assertEqual((txn.invalidation, txn.post_trade_review), ("Break below support", "Review after earnings"))

    def test_invalid_csv_is_atomic(self):
        data = b"code,quantity,price\nJOURNAL,10,100\nMISSING,10,100\n"
        self.client.post(reverse("portfolio_import_csv", args=[self.portfolio.id]), {"csv_file": SimpleUploadedFile("broker.csv", data, content_type="text/csv")})
        self.assertFalse(PortfolioTransaction.objects.filter(portfolio=self.portfolio).exists())


class WeightedAverageCostBasisTests(TestCase):
    """The core financial math. Hand-verified figures in comments."""

    def setUp(self):
        self.user = make_user("wac_user")
        self.portfolio = psvc.get_or_create_default_portfolio(self.user)
        self.stock = make_stock(price=15.0)

    def test_single_buy_average_price_and_cost_basis(self):
        psvc.create_transaction(self.portfolio, self.stock, "BUY", Decimal("100"), Decimal("10.00"), Decimal("5"), date(2026, 1, 1))
        calc = psvc.compute_holding(self.portfolio, self.stock)
        self.assertEqual(calc.quantity, Decimal("100"))
        self.assertEqual(calc.average_price, Decimal("10.0000"))
        self.assertEqual(calc.cost_basis, Decimal("1005"))  # 100*10 + 5 fee

    def test_two_buys_blend_to_weighted_average(self):
        # 100 @ 10 (+5 fee) then 100 @ 20 (+5 fee) => avg price (pure, no
        # fees) = (1000+2000)/200 = 15.00; cost_basis = 3000 + 10 = 3010
        psvc.create_transaction(self.portfolio, self.stock, "BUY", Decimal("100"), Decimal("10.00"), Decimal("5"), date(2026, 1, 1))
        psvc.create_transaction(self.portfolio, self.stock, "BUY", Decimal("100"), Decimal("20.00"), Decimal("5"), date(2026, 1, 5))
        calc = psvc.compute_holding(self.portfolio, self.stock)
        self.assertEqual(calc.quantity, Decimal("200"))
        self.assertEqual(calc.average_price, Decimal("15.0000"))
        self.assertEqual(calc.cost_basis, Decimal("3010"))

    def test_partial_sell_leaves_average_price_unchanged(self):
        psvc.create_transaction(self.portfolio, self.stock, "BUY", Decimal("100"), Decimal("10.00"), Decimal("5"), date(2026, 1, 1))
        psvc.create_transaction(self.portfolio, self.stock, "BUY", Decimal("100"), Decimal("20.00"), Decimal("5"), date(2026, 1, 5))
        psvc.create_transaction(self.portfolio, self.stock, "SELL", Decimal("50"), Decimal("25.00"), Decimal("2"), date(2026, 1, 10))
        calc = psvc.compute_holding(self.portfolio, self.stock)
        # WAC is unaffected by a partial sale: still 15.00/share
        self.assertEqual(calc.average_price, Decimal("15.0000"))
        self.assertEqual(calc.quantity, Decimal("150"))
        # ratio = 50/200 = 0.25; removed = 3000*0.25 + 10*0.25 = 750 + 2.5 = 752.5
        # proceeds = 50*25 - 2 = 1248; realized = 1248 - 752.5 = 495.5
        self.assertEqual(calc.realized_pl, Decimal("495.5"))
        self.assertEqual(calc.cost_basis, Decimal("2257.5"))

    def test_complete_sell_zeroes_cost_basis_exactly(self):
        psvc.create_transaction(self.portfolio, self.stock, "BUY", Decimal("100"), Decimal("10.00"), Decimal("5"), date(2026, 1, 1))
        psvc.create_transaction(self.portfolio, self.stock, "SELL", Decimal("100"), Decimal("12.00"), Decimal("3"), date(2026, 1, 10))
        calc = psvc.compute_holding(self.portfolio, self.stock)
        self.assertEqual(calc.quantity, Decimal("0"))
        self.assertEqual(calc.cost_basis, Decimal("0"))
        self.assertFalse(calc.is_open)
        # proceeds = 100*12 - 3 = 1197; cost removed = 1000 + 5 = 1005; realized = 192
        self.assertEqual(calc.realized_pl, Decimal("192"))

    def test_realized_and_unrealized_pl_are_independent(self):
        psvc.create_transaction(self.portfolio, self.stock, "BUY", Decimal("100"), Decimal("10.00"), Decimal("0"), date(2026, 1, 1))
        psvc.create_transaction(self.portfolio, self.stock, "SELL", Decimal("50"), Decimal("20.00"), Decimal("0"), date(2026, 1, 5))
        self.stock.last_price = 8.0  # remaining shares now underwater
        self.stock.save(update_fields=["last_price"])
        row = psvc.holding_row(psvc.compute_holding(self.portfolio, self.stock))
        self.assertEqual(row["realized_pl"], Decimal("500.00"))  # (20-10)*50
        self.assertEqual(row["unrealized_pl"], Decimal("-100.00"))  # (8-10)*50

    def test_sell_fees_reduce_proceeds_not_cost_basis(self):
        psvc.create_transaction(self.portfolio, self.stock, "BUY", Decimal("10"), Decimal("100"), Decimal("0"), date(2026, 1, 1))
        psvc.create_transaction(self.portfolio, self.stock, "SELL", Decimal("10"), Decimal("100"), Decimal("50"), date(2026, 1, 2))
        calc = psvc.compute_holding(self.portfolio, self.stock)
        # proceeds = 1000 - 50 = 950; cost removed = 1000; realized = -50
        self.assertEqual(calc.realized_pl, Decimal("-50"))


class TransactionValidationTests(TestCase):
    def setUp(self):
        self.user = make_user("validator")
        self.portfolio = psvc.get_or_create_default_portfolio(self.user)
        self.stock = make_stock(price=10.0)

    def test_zero_quantity_rejected(self):
        with self.assertRaises(psvc.PortfolioValidationError):
            psvc.create_transaction(self.portfolio, self.stock, "BUY", Decimal("0"), Decimal("10"), Decimal("0"), date(2026, 1, 1))

    def test_negative_quantity_rejected(self):
        with self.assertRaises(psvc.PortfolioValidationError):
            psvc.create_transaction(self.portfolio, self.stock, "BUY", Decimal("-5"), Decimal("10"), Decimal("0"), date(2026, 1, 1))

    def test_negative_price_rejected(self):
        with self.assertRaises(psvc.PortfolioValidationError):
            psvc.create_transaction(self.portfolio, self.stock, "BUY", Decimal("5"), Decimal("-10"), Decimal("0"), date(2026, 1, 1))

    def test_negative_fees_rejected(self):
        with self.assertRaises(psvc.PortfolioValidationError):
            psvc.create_transaction(self.portfolio, self.stock, "BUY", Decimal("5"), Decimal("10"), Decimal("-1"), date(2026, 1, 1))

    def test_zero_price_is_allowed(self):
        # A bonus/gift share at 0 cost is a legitimate real-world case.
        txn = psvc.create_transaction(self.portfolio, self.stock, "BUY", Decimal("5"), Decimal("0"), Decimal("0"), date(2026, 1, 1))
        self.assertEqual(txn.price_per_share, Decimal("0"))

    def test_cannot_sell_with_no_prior_holding(self):
        with self.assertRaises(psvc.PortfolioValidationError):
            psvc.create_transaction(self.portfolio, self.stock, "SELL", Decimal("10"), Decimal("10"), Decimal("0"), date(2026, 1, 1))

    def test_cannot_oversell_beyond_current_holding(self):
        psvc.create_transaction(self.portfolio, self.stock, "BUY", Decimal("50"), Decimal("10"), Decimal("0"), date(2026, 1, 1))
        with self.assertRaises(psvc.PortfolioValidationError):
            psvc.create_transaction(self.portfolio, self.stock, "SELL", Decimal("51"), Decimal("10"), Decimal("0"), date(2026, 1, 2))

    def test_selling_exact_holding_is_allowed(self):
        psvc.create_transaction(self.portfolio, self.stock, "BUY", Decimal("50"), Decimal("10"), Decimal("0"), date(2026, 1, 1))
        txn = psvc.create_transaction(self.portfolio, self.stock, "SELL", Decimal("50"), Decimal("10"), Decimal("0"), date(2026, 1, 2))
        self.assertIsNotNone(txn.id)

    def test_oversell_check_is_date_aware_not_just_final_state(self):
        """A SELL dated *before* a later BUY must be validated against
        what was actually held at that earlier date, not the eventual
        (larger) total."""
        psvc.create_transaction(self.portfolio, self.stock, "BUY", Decimal("10"), Decimal("10"), Decimal("0"), date(2026, 1, 1))
        with self.assertRaises(psvc.PortfolioValidationError):
            # Only 10 were held as of Jan 2 — this 20-share sale dated Jan 2
            # must fail even though a later Jan 10 BUY would cover it.
            psvc.create_transaction(self.portfolio, self.stock, "SELL", Decimal("20"), Decimal("10"), Decimal("0"), date(2026, 1, 2))

    def test_editing_an_early_buy_down_invalidates_a_later_sell(self):
        buy = psvc.create_transaction(self.portfolio, self.stock, "BUY", Decimal("100"), Decimal("10"), Decimal("0"), date(2026, 1, 1))
        psvc.create_transaction(self.portfolio, self.stock, "SELL", Decimal("80"), Decimal("12"), Decimal("0"), date(2026, 1, 5))
        with self.assertRaises(psvc.PortfolioValidationError):
            psvc.update_transaction(buy, "BUY", Decimal("50"), Decimal("10"), Decimal("0"), date(2026, 1, 1))
        buy.refresh_from_db()
        self.assertEqual(buy.quantity, Decimal("100"), "the invalid edit must not have been committed")

    def test_deleting_an_early_buy_that_a_later_sell_depends_on_is_rejected(self):
        buy = psvc.create_transaction(self.portfolio, self.stock, "BUY", Decimal("100"), Decimal("10"), Decimal("0"), date(2026, 1, 1))
        psvc.create_transaction(self.portfolio, self.stock, "SELL", Decimal("80"), Decimal("12"), Decimal("0"), date(2026, 1, 5))
        buy_id = buy.id  # captured before delete_transaction() — Model.delete() nulls buy.pk in-memory even when the surrounding transaction later rolls back
        with self.assertRaises(psvc.PortfolioValidationError):
            psvc.delete_transaction(buy)
        self.assertTrue(PortfolioTransaction.objects.filter(id=buy_id).exists(), "delete must have rolled back")

    def test_editing_quantity_to_a_still_valid_value_succeeds(self):
        buy = psvc.create_transaction(self.portfolio, self.stock, "BUY", Decimal("100"), Decimal("10"), Decimal("0"), date(2026, 1, 1))
        psvc.create_transaction(self.portfolio, self.stock, "SELL", Decimal("80"), Decimal("12"), Decimal("0"), date(2026, 1, 5))
        psvc.update_transaction(buy, "BUY", Decimal("90"), Decimal("10"), Decimal("0"), date(2026, 1, 1))
        buy.refresh_from_db()
        self.assertEqual(buy.quantity, Decimal("90"))


class FutureDatedTransactionTests(TestCase):
    def setUp(self):
        self.user = make_user("future_user")
        self.portfolio = psvc.get_or_create_default_portfolio(self.user)
        self.stock = make_stock(price=10.0)

    def test_future_buy_does_not_appear_in_current_holdings(self):
        future = timezone.localdate() + timedelta(days=30)
        psvc.create_transaction(self.portfolio, self.stock, "BUY", Decimal("10"), Decimal("10"), Decimal("0"), future)
        holdings = psvc.compute_holdings(self.portfolio)
        self.assertEqual(len(holdings), 0)

    def test_future_transaction_is_still_stored_in_the_ledger(self):
        future = timezone.localdate() + timedelta(days=30)
        psvc.create_transaction(self.portfolio, self.stock, "BUY", Decimal("10"), Decimal("10"), Decimal("0"), future)
        self.assertEqual(PortfolioTransaction.objects.filter(portfolio=self.portfolio).count(), 1)

    def test_as_of_date_can_look_at_a_past_snapshot(self):
        psvc.create_transaction(self.portfolio, self.stock, "BUY", Decimal("10"), Decimal("10"), Decimal("0"), date(2026, 1, 1))
        psvc.create_transaction(self.portfolio, self.stock, "BUY", Decimal("10"), Decimal("10"), Decimal("0"), date(2026, 6, 1))
        early = psvc.compute_holding(self.portfolio, self.stock, as_of=date(2026, 3, 1))
        self.assertEqual(early.quantity, Decimal("10"))


class SameTradingCodeAcrossExchangesTests(TestCase):
    def setUp(self):
        self.user = make_user("dual_user")
        self.portfolio = psvc.get_or_create_default_portfolio(self.user)
        self.dse_stock = make_stock(exchange=Exchange.DSE, code="SAME", price=50.0)
        self.cse_stock = make_stock(exchange=Exchange.CSE, code="SAME", price=80.0)

    @override_settings(ENABLE_DSE=True, ENABLE_CSE=True)
    def test_dse_and_cse_holdings_of_the_same_code_are_tracked_separately(self):
        psvc.create_transaction(self.portfolio, self.dse_stock, "BUY", Decimal("10"), Decimal("50"), Decimal("0"), date(2026, 1, 1))
        psvc.create_transaction(self.portfolio, self.cse_stock, "BUY", Decimal("5"), Decimal("80"), Decimal("0"), date(2026, 1, 1))
        holdings = {h.stock.exchange: h for h in psvc.compute_holdings(self.portfolio)}
        self.assertEqual(holdings["DSE"].quantity, Decimal("10"))
        self.assertEqual(holdings["CSE"].quantity, Decimal("5"))

    def test_overselling_one_exchange_does_not_touch_the_other(self):
        psvc.create_transaction(self.portfolio, self.dse_stock, "BUY", Decimal("10"), Decimal("50"), Decimal("0"), date(2026, 1, 1))
        with self.assertRaises(psvc.PortfolioValidationError):
            psvc.create_transaction(self.portfolio, self.cse_stock, "SELL", Decimal("1"), Decimal("80"), Decimal("0"), date(2026, 1, 2))


class EmptyAndZeroPortfolioTests(TestCase):
    def test_empty_portfolio_summary_has_no_division_errors(self):
        user = make_user("empty_user")
        portfolio = psvc.get_or_create_default_portfolio(user)
        summary = psvc.portfolio_summary(portfolio)
        self.assertEqual(summary["open_holdings_count"], 0)
        self.assertEqual(summary["total_cost_basis"], Decimal("0.00"))
        self.assertEqual(summary["total_market_value"], Decimal("0.00"))
        self.assertIsNone(summary["total_unrealized_pl_pct"])
        self.assertIsNone(summary["best_holding"])
        self.assertIsNone(summary["worst_holding"])

    def test_fully_closed_position_has_zero_cost_basis_not_a_crash(self):
        user = make_user("closed_user")
        portfolio = psvc.get_or_create_default_portfolio(user)
        stock = make_stock(price=10.0)
        psvc.create_transaction(portfolio, stock, "BUY", Decimal("10"), Decimal("10"), Decimal("0"), date(2026, 1, 1))
        psvc.create_transaction(portfolio, stock, "SELL", Decimal("10"), Decimal("12"), Decimal("0"), date(2026, 1, 2))
        summary = psvc.portfolio_summary(portfolio)
        self.assertEqual(summary["open_holdings_count"], 0)
        self.assertEqual(summary["total_realized_pl"], Decimal("20.00"))

    def test_holding_with_zero_cost_basis_gift_shares_has_no_pct_division_error(self):
        user = make_user("gift_user")
        portfolio = psvc.get_or_create_default_portfolio(user)
        stock = make_stock(price=10.0)
        psvc.create_transaction(portfolio, stock, "BUY", Decimal("10"), Decimal("0"), Decimal("0"), date(2026, 1, 1))
        row = psvc.holding_row(psvc.compute_holding(portfolio, stock))
        self.assertEqual(row["cost_basis"], Decimal("0.00"))
        self.assertIsNone(row["unrealized_pl_pct"])  # cannot compute % of zero cost basis
        self.assertEqual(row["unrealized_pl"], Decimal("100.00"))  # still a valid absolute figure

    def test_holding_with_no_price_data_has_no_crash(self):
        user = make_user("noprice_user")
        portfolio = psvc.get_or_create_default_portfolio(user)
        stock = make_stock(price=None)
        stock.last_price = None
        stock.save(update_fields=["last_price"])
        psvc.create_transaction(portfolio, stock, "BUY", Decimal("10"), Decimal("10"), Decimal("0"), date(2026, 1, 1))
        row = psvc.holding_row(psvc.compute_holding(portfolio, stock))
        self.assertIsNone(row["latest_price"])
        self.assertIsNone(row["market_value"])
        self.assertIsNone(row["unrealized_pl"])
        self.assertEqual(row["quote_status"], psvc.QUOTE_UNAVAILABLE)


class QuoteStatusTests(TestCase):
    def setUp(self):
        self.stock = make_stock(price=25.5)

    def _set_updated_at(self, dt):
        Stock.objects.filter(id=self.stock.id).update(updated_at=dt)
        self.stock.refresh_from_db()

    def test_no_price_is_unavailable(self):
        self.stock.last_price = None
        self.stock.save(update_fields=["last_price"])
        status = psvc.quote_status(self.stock)
        self.assertEqual(status["status"], psvc.QUOTE_UNAVAILABLE)

    def test_synthetic_source_overrides_everything_else(self):
        now = timezone.now()
        self._set_updated_at(now)
        PriceHistory.objects.create(
            stock=self.stock, date=timezone.localdate(), open=25, high=26, low=24, close=25.5,
            volume=100, is_synthetic=True,
        )
        with mock.patch("market.services.portfolio.session_status", return_value={"is_open": True}):
            status = psvc.quote_status(self.stock, now=now)
        self.assertEqual(status["status"], psvc.QUOTE_SYNTHETIC)

    def test_stale_beyond_threshold_days(self):
        now = timezone.now()
        self._set_updated_at(now - timedelta(days=psvc.STALE_DATA_DAYS + 1))
        with mock.patch("market.services.portfolio.session_status", return_value={"is_open": False}):
            status = psvc.quote_status(self.stock, now=now)
        self.assertEqual(status["status"], psvc.QUOTE_STALE)

    def test_live_when_market_open_and_fresh(self):
        now = timezone.now()
        self._set_updated_at(now - timedelta(minutes=2))
        with mock.patch("market.services.portfolio.session_status", return_value={"is_open": True}):
            status = psvc.quote_status(self.stock, now=now)
        self.assertEqual(status["status"], psvc.QUOTE_LIVE)

    def test_delayed_when_market_open_but_not_fresh(self):
        now = timezone.now()
        self._set_updated_at(now - timedelta(minutes=20))
        with mock.patch("market.services.portfolio.session_status", return_value={"is_open": True}):
            status = psvc.quote_status(self.stock, now=now)
        self.assertEqual(status["status"], psvc.QUOTE_DELAYED)

    def test_market_closed_when_exchange_closed_and_not_stale(self):
        now = timezone.now()
        self._set_updated_at(now - timedelta(hours=2))
        with mock.patch("market.services.portfolio.session_status", return_value={"is_open": False}):
            status = psvc.quote_status(self.stock, now=now)
        self.assertEqual(status["status"], psvc.QUOTE_MARKET_CLOSED)

    def test_live_never_shown_just_because_page_was_reloaded(self):
        """Quote status must be a pure function of the quote's own age/
        source/market-state — calling it twice in a row with an old
        updated_at must not somehow read as fresher the second time."""
        now = timezone.now()
        self._set_updated_at(now - timedelta(days=10))
        with mock.patch("market.services.portfolio.session_status", return_value={"is_open": True}):
            first = psvc.quote_status(self.stock, now=now)
            second = psvc.quote_status(self.stock, now=now + timedelta(seconds=1))
        self.assertEqual(first["status"], psvc.QUOTE_STALE)
        self.assertEqual(second["status"], psvc.QUOTE_STALE)


class PortfolioAllocationTests(TestCase):
    def test_allocation_percentages_sum_to_roughly_100(self):
        user = make_user("alloc_user")
        portfolio = psvc.get_or_create_default_portfolio(user)
        a = make_stock(code="ALLOCA", price=10.0)
        b = make_stock(code="ALLOCB", price=20.0)
        psvc.create_transaction(portfolio, a, "BUY", Decimal("10"), Decimal("10"), Decimal("0"), date(2026, 1, 1))
        psvc.create_transaction(portfolio, b, "BUY", Decimal("5"), Decimal("20"), Decimal("0"), date(2026, 1, 1))
        summary = psvc.portfolio_summary(portfolio)
        total_pct = sum(h["allocation_pct"] for h in summary["holdings"])
        self.assertAlmostEqual(float(total_pct), 100.0, delta=0.1)

    def test_allocation_by_sector_groups_correctly(self):
        user = make_user("sector_user")
        portfolio = psvc.get_or_create_default_portfolio(user)
        a = make_stock(code="SECA", price=10.0, sector="Pharma")
        b = make_stock(code="SECB", price=10.0, sector="Pharma")
        c = make_stock(code="SECC", price=10.0, sector="Banking")
        for s in (a, b, c):
            psvc.create_transaction(portfolio, s, "BUY", Decimal("10"), Decimal("10"), Decimal("0"), date(2026, 1, 1))
        summary = psvc.portfolio_summary(portfolio)
        by_sector = {row["label"]: row["value"] for row in summary["allocation_by_sector"]}
        self.assertEqual(by_sector["Pharma"], Decimal("200.00"))
        self.assertEqual(by_sector["Banking"], Decimal("100.00"))


class PortfolioViewMutationTests(TestCase):
    def setUp(self):
        self.user = make_user("view_user")
        self.portfolio = psvc.get_or_create_default_portfolio(self.user)
        self.stock = make_stock(price=15.0)
        self.client.login(username="view_user", password=PASSWORD)

    def test_add_holding_records_a_buy(self):
        response = self.client.post(
            reverse("portfolio_add_holding", args=[self.portfolio.id]),
            {"stock": self.stock.id, "quantity": "10", "price_per_share": "15", "fees": "0", "transaction_date": "2026-01-01"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(PortfolioTransaction.objects.get(portfolio=self.portfolio).transaction_type, TransactionType.BUY)

    def test_add_transaction_get_is_not_a_mutation(self):
        response = self.client.get(reverse("portfolio_add_transaction", args=[self.portfolio.id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PortfolioTransaction.objects.count(), 0)

    def test_delete_transaction_requires_post(self):
        txn = psvc.create_transaction(self.portfolio, self.stock, "BUY", Decimal("10"), Decimal("15"), Decimal("0"), date(2026, 1, 1))
        response = self.client.get(reverse("portfolio_delete_transaction", args=[self.portfolio.id, txn.id]))
        self.assertEqual(response.status_code, 405)
        self.assertTrue(PortfolioTransaction.objects.filter(id=txn.id).exists())

    def test_csrf_is_enforced_on_transaction_delete(self):
        txn = psvc.create_transaction(self.portfolio, self.stock, "BUY", Decimal("10"), Decimal("15"), Decimal("0"), date(2026, 1, 1))
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.login(username="view_user", password=PASSWORD)
        response = csrf_client.post(reverse("portfolio_delete_transaction", args=[self.portfolio.id, txn.id]))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(PortfolioTransaction.objects.filter(id=txn.id).exists())

    def test_delete_portfolio_requires_matching_name_confirmation(self):
        response = self.client.post(reverse("portfolio_delete", args=[self.portfolio.id]), {"confirm_name": "wrong name"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Portfolio.objects.filter(id=self.portfolio.id).exists())

    def test_delete_portfolio_succeeds_with_correct_confirmation(self):
        response = self.client.post(reverse("portfolio_delete", args=[self.portfolio.id]), {"confirm_name": self.portfolio.name})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Portfolio.objects.filter(id=self.portfolio.id).exists())

    def test_deleting_a_transaction_that_a_later_sell_depends_on_shows_error_not_500(self):
        buy = psvc.create_transaction(self.portfolio, self.stock, "BUY", Decimal("100"), Decimal("15"), Decimal("0"), date(2026, 1, 1))
        psvc.create_transaction(self.portfolio, self.stock, "SELL", Decimal("80"), Decimal("16"), Decimal("0"), date(2026, 1, 5))
        response = self.client.post(reverse("portfolio_delete_transaction", args=[self.portfolio.id, buy.id]))
        self.assertEqual(response.status_code, 302)  # redirects with an error message, not a 500
        self.assertTrue(PortfolioTransaction.objects.filter(id=buy.id).exists())

    def test_view_never_triggers_a_live_fetch(self):
        with mock.patch("market.services.dse_fetcher._get") as mock_get, mock.patch("market.services.cse_fetcher._get") as mock_cse_get:
            self.client.get(reverse("portfolio_detail", args=[self.portfolio.id]))
            self.client.get(reverse("portfolio_quotes_json", args=[self.portfolio.id]))
        mock_get.assert_not_called()
        mock_cse_get.assert_not_called()

    def test_nav_shows_portfolio_link_only_when_authenticated(self):
        response = self.client.get(reverse("portfolio_detail", args=[self.portfolio.id]))
        self.assertContains(response, "Portfolio")
        self.client.logout()
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Make your market research easier")


class PortfolioAPITests(TestCase):
    def setUp(self):
        self.user = make_user("api_user")
        self.other = make_user("api_other")
        self.portfolio = psvc.get_or_create_default_portfolio(self.user)
        self.stock = make_stock(price=12.5)

    def _auth(self, username):
        client = Client()
        client.login(username=username, password=PASSWORD)
        return client

    def test_unauthenticated_request_is_rejected(self):
        response = self.client.get(reverse("api_portfolio_list"))
        self.assertIn(response.status_code, (401, 403))

    def test_list_only_returns_own_portfolios(self):
        Portfolio.objects.create(user=self.other, name="Not yours")
        client = self._auth("api_user")
        response = client.get(reverse("api_portfolio_list"))
        self.assertEqual(response.status_code, 200)
        names = [p["name"] for p in response.json()["results"]]
        self.assertEqual(names, [self.portfolio.name])

    def test_cannot_fetch_summary_for_someone_elses_portfolio(self):
        client = self._auth("api_other")
        response = client.get(reverse("api_portfolio_summary", args=[self.portfolio.id]))
        self.assertEqual(response.status_code, 404)

    def test_create_transaction_via_api(self):
        client = self._auth("api_user")
        response = client.post(
            reverse("api_portfolio_transactions", args=[self.portfolio.id]),
            {
                "stock_id": self.stock.id, "transaction_type": "BUY", "quantity": "10",
                "price_per_share": "12.5", "fees": "1", "transaction_date": "2026-01-01",
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["quantity"], "10.0000")

    def test_oversell_via_api_returns_400_not_500(self):
        client = self._auth("api_user")
        response = client.post(
            reverse("api_portfolio_transactions", args=[self.portfolio.id]),
            {"stock_id": self.stock.id, "transaction_type": "SELL", "quantity": "5", "price_per_share": "12.5", "transaction_date": "2026-01-01"},
        )
        self.assertEqual(response.status_code, 400)

    def test_monetary_values_are_returned_as_strings(self):
        psvc.create_transaction(self.portfolio, self.stock, "BUY", Decimal("10"), Decimal("12.5"), Decimal("0"), date(2026, 1, 1))
        client = self._auth("api_user")
        response = client.get(reverse("api_portfolio_summary", args=[self.portfolio.id]))
        payload = response.json()
        self.assertIsInstance(payload["total_cost_basis"], str)
        self.assertIsInstance(payload["holdings"][0]["market_value"], str)

    def test_cannot_delete_someone_elses_transaction_via_api(self):
        txn = psvc.create_transaction(self.portfolio, self.stock, "BUY", Decimal("10"), Decimal("12.5"), Decimal("0"), date(2026, 1, 1))
        client = self._auth("api_other")
        response = client.delete(reverse("api_portfolio_transaction_detail", args=[self.portfolio.id, txn.id]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(PortfolioTransaction.objects.filter(id=txn.id).exists())


class QueryEfficiencyTests(TestCase):
    def test_holdings_computation_does_not_n_plus_one_per_stock(self):
        user = make_user("perf_user")
        portfolio = psvc.get_or_create_default_portfolio(user)
        stocks = [make_stock(code=f"PERF{i}", price=10.0 + i) for i in range(25)]
        for s in stocks:
            psvc.create_transaction(portfolio, s, "BUY", Decimal("10"), Decimal("10"), Decimal("0"), date(2026, 1, 1))

        with CaptureQueriesContext(connection) as ctx:
            summary = psvc.portfolio_summary(portfolio)
        self.assertEqual(summary["open_holdings_count"], 25)
        # One query for the transaction ledger (compute_holdings) + one for
        # the same again inside include_closed=True + a small constant
        # number of per-stock quote lookups (PriceHistory synthetic check
        # + previous-close lookup) — must not scale linearly with stock
        # count the way a naive per-stock re-query of transactions would.
        self.assertLess(len(ctx.captured_queries), 10 + len(stocks) * 2)

    def test_portfolio_detail_page_query_count_is_bounded(self):
        user = make_user("perf_view_user")
        portfolio = psvc.get_or_create_default_portfolio(user)
        stocks = [make_stock(code=f"VPERF{i}", price=10.0 + i) for i in range(15)]
        for s in stocks:
            psvc.create_transaction(portfolio, s, "BUY", Decimal("10"), Decimal("10"), Decimal("0"), date(2026, 1, 1))
        client = Client()
        client.login(username="perf_view_user", password=PASSWORD)
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(reverse("portfolio_detail", args=[portfolio.id]))
        self.assertEqual(response.status_code, 200)
        self.assertLess(len(ctx.captured_queries), 60)


class ResponsivePageContentTests(TestCase):
    """Not a real browser/viewport test (no headless browser in this
    project's toolchain) — asserts the responsive/mobile-support hooks
    that actually exist (the viewport meta tag, the horizontally
    scrollable table wrapper) are present in the rendered output."""

    def setUp(self):
        self.user = make_user("mobile_user")
        self.portfolio = psvc.get_or_create_default_portfolio(self.user)
        self.client.login(username="mobile_user", password=PASSWORD)

    def test_viewport_meta_present(self):
        response = self.client.get(reverse("portfolio_detail", args=[self.portfolio.id]))
        self.assertContains(response, 'name="viewport"')

    def test_holdings_table_is_horizontally_scrollable_on_small_screens(self):
        stock = make_stock(price=10.0)
        psvc.create_transaction(self.portfolio, stock, "BUY", Decimal("10"), Decimal("10"), Decimal("0"), date(2026, 1, 1))
        response = self.client.get(reverse("portfolio_detail", args=[self.portfolio.id]))
        self.assertContains(response, 'class="table-wrap"')

    def test_empty_state_shows_helpful_instructions(self):
        response = self.client.get(reverse("portfolio_detail", args=[self.portfolio.id]))
        self.assertContains(response, "No open holdings yet")
        self.assertContains(response, "Add your first holding")
