from datetime import date, datetime, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from market.models import (
    AnalysisResult,
    Exchange,
    PaperEquitySnapshot,
    PaperLearningFeedback,
    PaperCashSettlement,
    PaperPosition,
    PaperTrade,
    PriceHistory,
    SignalAction,
    Stock,
)
from market.services.paper_trading import ensure_account, run_autonomous_cycle, trading_window_status


class PaperTradingServiceTests(TestCase):
    def setUp(self):
        self.day = date(2026, 8, 3)
        self.stock = Stock.objects.create(
            exchange=Exchange.DSE, trading_code="PAPER", company_name="Paper Ltd", last_price=100, last_volume=100000
        )
        for i in range(15):
            d = self.day - timedelta(days=20 - i)
            PriceHistory.objects.create(
                stock=self.stock, date=d, open=100, high=102, low=99, close=100, volume=100000
            )
        self.buy_signal = AnalysisResult.objects.create(
            stock=self.stock, as_of=self.day, action=SignalAction.BUY, score=60,
            confidence=0.75, probability=0.70, risk_level="low", is_safe_buy=True,
        )

    def test_starts_with_100000_and_buys_only_a_small_virtual_position(self):
        result = run_autonomous_cycle(as_of=self.day)
        account = ensure_account()
        position = PaperPosition.objects.get(is_open=True)

        self.assertTrue(result["ok"])
        self.assertEqual(account.initial_cash, Decimal("100000.00"))
        self.assertLess(position.quantity * position.entry_price, Decimal("5100"))
        self.assertGreater(account.cash, Decimal("94000"))
        self.assertEqual(PaperTrade.objects.get().side, PaperTrade.Side.BUY)
        self.assertGreater(PaperTrade.objects.get().execution_price, Decimal("100"))  # adverse slippage

    def test_daily_cycle_is_idempotent(self):
        run_autonomous_cycle(as_of=self.day)
        result = run_autonomous_cycle(as_of=self.day)
        self.assertTrue(result["ok"])
        self.assertEqual(PaperTrade.objects.count(), 1)
        self.assertEqual(PaperEquitySnapshot.objects.count(), 1)

    def test_sell_prediction_closes_position_and_records_realized_pnl(self):
        run_autonomous_cycle(as_of=self.day)
        position = PaperPosition.objects.get()
        next_day = position.maturity_date
        AnalysisResult.objects.create(
            stock=self.stock, as_of=next_day, action=SignalAction.SELL, score=-50,
            confidence=0.8, probability=0.2, risk_level="medium", is_safe_buy=False,
        )
        self.stock.last_price = 110
        self.stock.save(update_fields=["last_price"])

        run_autonomous_cycle(as_of=next_day)
        position = PaperPosition.objects.get()
        self.assertFalse(position.is_open)
        self.assertEqual(position.exit_reason, "sell_prediction")
        self.assertGreater(position.realized_pnl, 0)
        self.assertEqual(PaperTrade.objects.filter(side=PaperTrade.Side.SELL).count(), 1)
        feedback = PaperLearningFeedback.objects.get(position=position)
        self.assertEqual(feedback.predicted_probability, 0.70)
        self.assertTrue(feedback.profitable_after_costs)
        self.assertGreater(feedback.net_return_pct, 0)
        self.assertLess(ensure_account().cash, Decimal("100000.00"))  # proceeds remain unsettled
        self.assertEqual(PaperCashSettlement.objects.filter(is_settled=False).count(), 1)

    def test_weak_prediction_does_not_buy(self):
        self.buy_signal.probability = 0.55
        self.buy_signal.save(update_fields=["probability"])
        run_autonomous_cycle(as_of=self.day)
        self.assertFalse(PaperPosition.objects.exists())

    def test_unmatured_shares_cannot_be_sold(self):
        run_autonomous_cycle(as_of=self.day)
        position = PaperPosition.objects.get()
        before_maturity = self.day + timedelta(days=1)
        AnalysisResult.objects.create(
            stock=self.stock, as_of=before_maturity, action=SignalAction.SELL, score=-60,
            confidence=0.8, probability=0.1, risk_level="medium", is_safe_buy=False,
        )
        run_autonomous_cycle(as_of=before_maturity)
        position.refresh_from_db()
        self.assertTrue(position.is_open)
        self.assertEqual(PaperTrade.objects.filter(side=PaperTrade.Side.SELL).count(), 0)


class PaperTradingWindowTests(TestCase):
    def _at(self, hour, minute):
        return timezone.make_aware(datetime(2026, 8, 6, hour, minute))

    def test_opens_with_market_and_stops_five_minutes_before_close(self):
        self.assertFalse(trading_window_status(self._at(9, 59))["is_open"])
        self.assertTrue(trading_window_status(self._at(10, 0))["is_open"])
        self.assertTrue(trading_window_status(self._at(14, 24))["is_open"])
        status = trading_window_status(self._at(14, 25))
        self.assertFalse(status["is_open"])
        self.assertEqual(status["stops_at"], "14:25")


class PaperTradingAccessTests(TestCase):
    def test_anonymous_redirects_and_regular_or_staff_user_gets_403(self):
        url = reverse("paper_trading")
        self.assertEqual(self.client.get(url).status_code, 302)

        user = User.objects.create_user("normal", password="test-pass-123")
        self.client.force_login(user)
        self.assertEqual(self.client.get(url).status_code, 403)

        user.is_staff = True
        user.save(update_fields=["is_staff"])
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_superuser_can_view_and_pause_but_get_cannot_mutate(self):
        admin = User.objects.create_superuser("paperadmin", password="test-pass-123")
        self.client.force_login(admin)
        self.assertEqual(self.client.get(reverse("paper_trading")).status_code, 200)
        self.assertEqual(self.client.get(reverse("paper_trading_control")).status_code, 405)

        response = self.client.post(reverse("paper_trading_control"), {"action": "pause"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(ensure_account().is_active)
