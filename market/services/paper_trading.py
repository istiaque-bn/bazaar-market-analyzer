"""Autonomous admin paper trading using stored Bazaar signals and prices only.

There is deliberately no broker adapter, order endpoint, credential field, or
network call in this module. "Execution" means an immutable virtual trade in
the local database at the latest stored price plus conservative costs.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from market.models import (
    AnalysisResult,
    PaperEquitySnapshot,
    PaperCashSettlement,
    PaperLearningFeedback,
    PaperPosition,
    PaperTrade,
    PaperTradingAccount,
    SignalAction,
)

DEFAULT_CONFIG = {
    "min_probability": 0.60,
    "min_confidence": 0.60,
    "position_size_pct": 5.0,
    "max_positions": 10,
    "stop_loss_pct": 5.0,
    "take_profit_pct": 8.0,
    "max_holding_sessions": 10,
    "fee_pct_per_side": 0.35,
    "slippage_pct_per_side": 0.10,
    "liquidity_limit_pct": 5.0,
    "minimum_session_volume": 100,
}
MONEY = Decimal("0.01")
PRICE = Decimal("0.0001")


def _d(value) -> Decimal:
    return Decimal(str(value))


def _money(value) -> Decimal:
    return _d(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def ensure_account() -> PaperTradingAccount:
    account, created = PaperTradingAccount.objects.get_or_create(
        name="Admin autonomous paper fund",
        defaults={"initial_cash": Decimal("100000.00"), "cash": Decimal("100000.00"), "strategy_config": DEFAULT_CONFIG},
    )
    merged = {**DEFAULT_CONFIG, **(account.strategy_config or {})}
    if created or merged != account.strategy_config:
        account.strategy_config = merged
        account.save(update_fields=["strategy_config", "updated_at"])
    return account


def trading_window_status(now=None) -> dict:
    """Paper automation runs during the real session, stopping 5m early."""
    from market.services.market_hours import SESSION_CLOSE, SESSION_OPEN, session_status

    local_now = timezone.localtime(now) if now else timezone.localtime()
    stop_at = (datetime.combine(local_now.date(), SESSION_CLOSE) - timedelta(minutes=5)).time()
    exchange_open = session_status("DSE", now=local_now)["is_open"]
    is_open = bool(exchange_open and SESSION_OPEN <= local_now.time() < stop_at)
    return {
        "is_open": is_open,
        "opens_at": SESSION_OPEN.strftime("%H:%M"),
        "stops_at": stop_at.strftime("%H:%M"),
        "now": local_now.isoformat(),
    }


def _price(stock) -> Decimal | None:
    if stock.last_price is None or stock.last_price <= 0:
        return None
    return _d(stock.last_price)


def settlement_date(trade_date, stock) -> object:
    """A/B/G/N settle T+2; Z-category settles T+3, skipping closures."""
    from market.models import StockGroup
    from market.services.market_hours import TRADING_WEEKDAYS
    from market.services.trading_calendar import closure_reason

    sessions = 3 if stock.group == StockGroup.Z else 2
    current = trade_date
    found = 0
    while found < sessions:
        current += timedelta(days=1)
        if current.weekday() in TRADING_WEEKDAYS and closure_reason(current) is None:
            found += 1
    return current


def _settle_cash(account, as_of) -> int:
    due = account.cash_settlements.filter(is_settled=False, settlement_date__lte=as_of).select_for_update()
    count = 0
    for item in due:
        account.cash = _money(account.cash + item.amount)
        item.is_settled = True
        item.settled_at = timezone.now()
        item.save(update_fields=["is_settled", "settled_at"])
        count += 1
    return count


def _execution_price(market_price: Decimal, side: str, cfg: dict) -> Decimal:
    slip = _d(cfg["slippage_pct_per_side"]) / 100
    multiplier = Decimal("1") + slip if side == PaperTrade.Side.BUY else Decimal("1") - slip
    return (market_price * multiplier).quantize(PRICE, rounding=ROUND_HALF_UP)


def _fee(notional: Decimal, cfg: dict) -> Decimal:
    return _money(notional * _d(cfg["fee_pct_per_side"]) / 100)


def _holding_sessions(position: PaperPosition, as_of) -> int:
    return position.stock.prices.live().filter(date__gt=position.opened_on, date__lte=as_of).values("date").distinct().count()


def _latest_signal(stock_id: int, as_of):
    return AnalysisResult.objects.filter(stock_id=stock_id, as_of__lte=as_of).order_by("-as_of").first()


def _equity(account: PaperTradingAccount) -> tuple[Decimal, Decimal, Decimal]:
    holdings = Decimal("0")
    cost = Decimal("0")
    for pos in account.positions.filter(is_open=True).select_related("stock"):
        current = _price(pos.stock) or pos.entry_price
        holdings += current * pos.quantity
        cost += pos.entry_price * pos.quantity + pos.entry_fee
    holdings = _money(holdings)
    unsettled = account.cash_settlements.filter(is_settled=False).aggregate(v=Sum("amount"))["v"] or Decimal("0")
    return holdings, _money(account.cash + holdings + unsettled), _money(holdings - cost)


def account_summary(account: PaperTradingAccount | None = None) -> dict:
    account = account or ensure_account()
    holdings, total, unrealized = _equity(account)
    realized = account.positions.filter(is_open=False).aggregate(v=Sum("realized_pnl"))["v"] or Decimal("0")
    unsettled = account.cash_settlements.filter(is_settled=False).aggregate(v=Sum("amount"))["v"] or Decimal("0")
    return {
        "account": account,
        "cash": account.cash,
        "holdings_value": holdings,
        "total_equity": total,
        "total_return": _money(total - account.initial_cash),
        "total_return_pct": round(float((total / account.initial_cash - 1) * 100), 2) if account.initial_cash else 0.0,
        "realized_pnl": _money(realized),
        "unsettled_cash": _money(unsettled),
        "unrealized_pnl": unrealized,
        "open_positions": account.positions.filter(is_open=True).count(),
    }


def record_equity_snapshot(account=None, as_of=None) -> dict:
    """Upsert the day's professional cash/holdings/P&L closing statement."""
    account = account or ensure_account()
    as_of = as_of or timezone.localdate()
    summary = account_summary(account)
    PaperEquitySnapshot.objects.update_or_create(
        account=account, as_of=as_of,
        defaults={
            "cash": summary["cash"], "holdings_value": summary["holdings_value"],
            "total_equity": summary["total_equity"], "realized_pnl": summary["realized_pnl"],
            "unrealized_pnl": summary["unrealized_pnl"], "open_positions": summary["open_positions"],
        },
    )
    return summary


def _close_position(account, position, market_price, as_of, reason, signal, cfg):
    execution = _execution_price(market_price, PaperTrade.Side.SELL, cfg)
    notional = execution * position.quantity
    fee = _fee(notional, cfg)
    proceeds = _money(notional - fee)
    pnl = _money(proceeds - (position.entry_price * position.quantity + position.entry_fee))
    position.is_open = False
    position.closed_on = as_of
    position.exit_price = execution
    position.exit_fee = fee
    position.realized_pnl = pnl
    position.exit_reason = reason
    position.save(update_fields=["is_open", "closed_on", "exit_price", "exit_fee", "realized_pnl", "exit_reason", "updated_at"])
    settle_on = settlement_date(as_of, position.stock)
    trade = PaperTrade.objects.create(
        account=account, position=position, stock=position.stock, side=PaperTrade.Side.SELL,
        trade_date=as_of, settlement_date=settle_on, quantity=position.quantity, market_price=market_price,
        execution_price=execution, fee=fee, cash_effect=proceeds, reason=reason, signal=signal,
    )
    PaperCashSettlement.objects.create(account=account, trade=trade, amount=proceeds, settlement_date=settle_on)
    original_signal = position.signal
    entry_notional = position.entry_price * position.quantity
    gross_return_pct = float((execution / position.entry_price - 1) * 100)
    net_return_pct = float(pnl / (entry_notional + position.entry_fee) * 100)
    PaperLearningFeedback.objects.create(
        position=position,
        stock=position.stock,
        signal_date=position.opened_on,
        outcome_date=as_of,
        predicted_probability=original_signal.probability if original_signal else None,
        predicted_confidence=original_signal.confidence if original_signal else None,
        predicted_score=original_signal.score if original_signal else None,
        gross_return_pct=round(gross_return_pct, 4),
        net_return_pct=round(net_return_pct, 4),
        profitable_after_costs=pnl > 0,
        holding_sessions=_holding_sessions(position, as_of),
        exit_reason=reason,
    )


def _open_position(account, signal, market_price, budget, as_of, cfg):
    execution = _execution_price(market_price, PaperTrade.Side.BUY, cfg)
    fee_rate = _d(cfg["fee_pct_per_side"]) / 100
    quantity = int((budget / (execution * (1 + fee_rate))).to_integral_value(rounding=ROUND_DOWN))
    session_volume = int(signal.stock.last_volume or 0)
    if session_volume < int(cfg["minimum_session_volume"]):
        return None
    quantity = min(quantity, int(session_volume * float(cfg["liquidity_limit_pct"]) / 100))
    if quantity < 1:
        return None
    notional = execution * quantity
    fee = _fee(notional, cfg)
    cash_paid = _money(notional + fee)
    if cash_paid > account.cash:
        return None
    position = PaperPosition.objects.create(
        account=account, stock=signal.stock, quantity=quantity, entry_price=execution,
        entry_fee=fee, opened_on=as_of, maturity_date=settlement_date(as_of, signal.stock), signal=signal,
    )
    account.cash = _money(account.cash - cash_paid)
    PaperTrade.objects.create(
        account=account, position=position, stock=signal.stock, side=PaperTrade.Side.BUY,
        trade_date=as_of, settlement_date=position.maturity_date, quantity=quantity, market_price=market_price,
        execution_price=execution, fee=fee, cash_effect=-cash_paid,
        reason="safe_buy_prediction", signal=signal,
    )
    return position


@transaction.atomic
def run_autonomous_cycle(*, force: bool = False, as_of=None) -> dict:
    """Run one idempotent daily virtual cycle: exits first, then entries."""
    as_of = as_of or timezone.localdate()
    account = ensure_account()
    account = PaperTradingAccount.objects.select_for_update().get(pk=account.pk)
    if not account.is_active and not force:
        return {"ok": True, "skipped": "paused"}
    cfg = {**DEFAULT_CONFIG, **(account.strategy_config or {})}
    settled_cash = _settle_cash(account, as_of)
    sold = []
    for position in account.positions.filter(is_open=True).select_related("stock"):
        market_price = _price(position.stock)
        if market_price is None:
            continue
        signal = _latest_signal(position.stock_id, as_of)
        change_pct = float((market_price / position.entry_price - 1) * 100)
        reason = None
        if position.maturity_date and as_of < position.maturity_date:
            continue
        if signal and signal.action == SignalAction.SELL:
            reason = "sell_prediction"
        elif change_pct <= -float(cfg["stop_loss_pct"]):
            reason = "stop_loss"
        elif change_pct >= float(cfg["take_profit_pct"]):
            reason = "take_profit"
        elif _holding_sessions(position, as_of) >= int(cfg["max_holding_sessions"]):
            reason = "holding_period"
        if reason:
            _close_position(account, position, market_price, as_of, reason, signal, cfg)
            sold.append(position.stock.trading_code)

    holdings, total_equity, _ = _equity(account)
    slots = max(0, int(cfg["max_positions"]) - account.positions.filter(is_open=True).count())
    opened_stock_ids = set(account.positions.filter(is_open=True).values_list("stock_id", flat=True))
    # Never churn out and back into the same share during one session.
    traded_stock_ids_today = set(account.trades.filter(trade_date=as_of).values_list("stock_id", flat=True))
    latest_as_of = AnalysisResult.objects.filter(as_of__lte=as_of).order_by("-as_of").values_list("as_of", flat=True).first()
    candidates = AnalysisResult.objects.none()
    if latest_as_of and slots:
        from market.services.exchange_config import enabled_exchanges

        candidates = (
            AnalysisResult.objects.filter(
                as_of=latest_as_of, action=SignalAction.BUY, is_safe_buy=True,
                confidence__gte=float(cfg["min_confidence"]), probability__gte=float(cfg["min_probability"]),
                stock__is_active=True, stock__exchange__in=enabled_exchanges(),
            )
            .exclude(risk_level="high")
            .exclude(stock_id__in=opened_stock_ids | traded_stock_ids_today)
            .select_related("stock")
            .order_by("-probability", "-confidence", "-score")[:slots]
        )

    bought = []
    budget = _money(total_equity * _d(cfg["position_size_pct"]) / 100)
    for signal in candidates:
        market_price = _price(signal.stock)
        if market_price is None or account.cash < Decimal("100"):
            continue
        position = _open_position(account, signal, market_price, min(budget, account.cash), as_of, cfg)
        if position:
            bought.append(signal.stock.trading_code)

    account.last_run_at = timezone.now()
    account.save(update_fields=["cash", "last_run_at", "updated_at"])
    summary = record_equity_snapshot(account, as_of)
    return {"ok": True, "bought": bought, "sold": sold, "cash_settled": settled_cash, "equity": str(summary["total_equity"]), "cash": str(account.cash)}
