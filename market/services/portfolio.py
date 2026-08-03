"""
Portfolio holdings, cost basis, and profit/loss — all derived on demand
from the PortfolioTransaction ledger, never stored as an editable summary.

Cost basis method: weighted average cost (WAC), the standard approach for
a fungible position bought in multiple lots at different prices:

  - Every BUY adds (quantity x price) to a running "purchase cost" total
    and its fees to a running "fees" total. cost_basis = purchase_cost +
    fees at all times.
  - Every SELL removes a *proportional* slice of both running totals —
    proportional to (quantity sold / quantity held immediately before the
    sell) — so the average cost per share is unaffected by a partial
    sale. The removed slice becomes this sale's contribution to realized
    P/L; SELL fees reduce proceeds rather than adding to cost basis
    (standard treatment: buy-side costs capitalize into cost basis,
    sell-side costs reduce what you walked away with).
  - realized_pl accumulates across every SELL, independently of
    unrealized_pl (marked against the *current* holding only). Closing a
    position entirely (quantity_held -> 0) leaves cost_basis at exactly
    0, not a rounding-drifted near-zero, because the last SELL's ratio is
    always exactly 1.

All money math uses Decimal. Stock.last_price/last_change_pct are plain
Python floats (the rest of the app's convention — see market/models.py),
so they're converted via str() before touching Decimal, never passed to
Decimal() directly, to avoid inheriting binary float imprecision.

Nothing here performs a live network fetch — every price comes from
Stock.last_price / PriceHistory, both written exclusively by the existing
background sync pipeline (market.services.autosync, dse_fetcher,
cse_fetcher). A portfolio page view must never block on a live scrape.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from market.models import Portfolio, PortfolioTransaction, PriceHistory, Stock, TransactionType
from market.services.market_hours import session_status
from market.services.ops_alerts import STALE_DATA_DAYS

ZERO = Decimal("0")
CENTS = Decimal("0.01")

QUOTE_LIVE = "live"
QUOTE_DELAYED = "delayed"
QUOTE_STALE = "stale"
QUOTE_MARKET_CLOSED = "market_closed"
QUOTE_SYNTHETIC = "synthetic"
QUOTE_UNAVAILABLE = "unavailable"

QUOTE_LABELS = {
    QUOTE_LIVE: "Live",
    QUOTE_DELAYED: "Delayed",
    QUOTE_STALE: "Stale",
    QUOTE_MARKET_CLOSED: "Market closed",
    QUOTE_SYNTHETIC: "Demo/Synthetic",
    QUOTE_UNAVAILABLE: "Unavailable",
}

LIVE_FRESHNESS_MINUTES = 5  # matches static/js/ticker.js's own LIVE threshold


class PortfolioValidationError(ValueError):
    """Raised for any transaction that would be invalid — caller (view or
    API) is expected to catch this and surface .args[0] as a form/field
    error rather than a 500."""


def to_decimal(value, default: Decimal | None = ZERO) -> Decimal | None:
    """Safe float/str/int/Decimal -> Decimal, via str() so a FloatField
    value like 16.83 never inherits base-2 binary imprecision."""
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def quantize_money(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Portfolios
# ---------------------------------------------------------------------------


def get_or_create_default_portfolio(user) -> Portfolio:
    """Every user gets exactly one portfolio automatically the first time
    they touch this feature. Safe under concurrent requests: the unique
    constraint on (user, is_default=True) means a race loses cleanly with
    an IntegrityError, which we retry as a plain lookup."""
    existing = Portfolio.objects.filter(user=user, is_default=True).first()
    if existing:
        return existing
    any_portfolio = Portfolio.objects.filter(user=user).order_by("id").first()
    if any_portfolio:
        any_portfolio.is_default = True
        any_portfolio.save(update_fields=["is_default"])
        return any_portfolio
    try:
        with transaction.atomic():
            return Portfolio.objects.create(user=user, name="Default", is_default=True)
    except Exception:
        # Lost a race with another request creating the first portfolio.
        return Portfolio.objects.filter(user=user).order_by("id").first()


# ---------------------------------------------------------------------------
# Quote status
# ---------------------------------------------------------------------------


RECENT_BARS_LOOKBACK_DAYS = 15  # matches close_learn.next_trading_day's own generous holiday-cluster buffer


def bulk_recent_bars(stocks, today: date) -> dict:
    """One query for the last ~couple of weeks of PriceHistory across
    every stock passed in, grouped by stock id — the batched alternative
    to quote_status()/_previous_close() each doing their own per-stock
    query. A portfolio page with N holdings must not cost 2N queries just
    to know each stock's latest bar and previous close."""
    stock_ids = [s.id for s in stocks]
    if not stock_ids:
        return {}
    cutoff = today - timedelta(days=RECENT_BARS_LOOKBACK_DAYS)
    rows = list(
        PriceHistory.objects.filter(stock_id__in=stock_ids, date__gte=cutoff, date__lte=today)
        .order_by("stock_id", "-date")
        .only("id", "stock_id", "date", "close", "is_synthetic")
    )
    grouped: dict[int, list] = {}
    for row in rows:
        grouped.setdefault(row.stock_id, []).append(row)
    return grouped


def quote_status(stock: Stock, now=None, recent_bars=None) -> dict:
    """Honest label for what stock.last_price actually represents right
    now. Never says "Live" just because the page was recently loaded —
    it's purely a function of the quote's own timestamp, source, and the
    exchange's real session state.

    Precedence (first match wins):
      1. No price at all                         -> Unavailable
      2. Most recent price bar is synthetic/demo  -> Demo/Synthetic
      3. Older than STALE_DATA_DAYS               -> Stale
      4. Market open, refreshed within 5 minutes  -> Live
      5. Market open, older than 5 minutes        -> Delayed
      6. Market closed                            -> Market closed

    `recent_bars` (a stock_id -> [PriceHistory, ...] map from
    bulk_recent_bars, newest first) lets a caller iterating many stocks
    avoid a per-stock query; falls back to one query for a single-stock
    caller (e.g. the stock detail page) when omitted."""
    now = now or timezone.now()
    price = quantize_money(to_decimal(stock.last_price, default=None))
    if price is None:
        return {"status": QUOTE_UNAVAILABLE, "label": QUOTE_LABELS[QUOTE_UNAVAILABLE], "price": None, "as_of": None}

    if recent_bars is not None:
        bars = recent_bars.get(stock.id, [])
        latest_bar = bars[0] if bars else None
    else:
        latest_bar = PriceHistory.objects.filter(stock=stock).order_by("-date").first()
    if latest_bar is not None and latest_bar.is_synthetic:
        return {
            "status": QUOTE_SYNTHETIC,
            "label": QUOTE_LABELS[QUOTE_SYNTHETIC],
            "price": price,
            "as_of": stock.updated_at,
        }

    as_of = stock.updated_at
    age = (now - as_of) if as_of else None
    if age is not None and age.days > STALE_DATA_DAYS:
        return {"status": QUOTE_STALE, "label": QUOTE_LABELS[QUOTE_STALE], "price": price, "as_of": as_of}

    hours = session_status(stock.exchange, now=now)
    if hours.get("is_open"):
        if age is not None and age.total_seconds() <= LIVE_FRESHNESS_MINUTES * 60:
            return {"status": QUOTE_LIVE, "label": QUOTE_LABELS[QUOTE_LIVE], "price": price, "as_of": as_of}
        return {"status": QUOTE_DELAYED, "label": QUOTE_LABELS[QUOTE_DELAYED], "price": price, "as_of": as_of}

    return {"status": QUOTE_MARKET_CLOSED, "label": QUOTE_LABELS[QUOTE_MARKET_CLOSED], "price": price, "as_of": as_of}


def _previous_close(stock: Stock, today: date, recent_bars=None) -> Decimal | None:
    """Most recent real (non-synthetic) close strictly before `today` —
    the baseline for "today's" gain/loss, whether or not today's own bar
    has been archived yet."""
    if recent_bars is not None:
        bars = recent_bars.get(stock.id, [])
        row = next((b for b in bars if not b.is_synthetic and b.date < today), None)
    else:
        row = PriceHistory.objects.live().filter(stock=stock, date__lt=today).order_by("-date").first()
    return to_decimal(row.close, default=None) if row else None


# ---------------------------------------------------------------------------
# Holdings (weighted-average cost)
# ---------------------------------------------------------------------------


@dataclass
class HoldingCalc:
    stock: Stock
    quantity: Decimal = ZERO
    purchase_cost: Decimal = ZERO  # running: quantity*price for currently-held shares only
    fees_in_basis: Decimal = ZERO  # running: buy-side fees allocated to currently-held shares
    realized_pl: Decimal = ZERO  # accumulated across every SELL for this stock, this portfolio
    total_fees_paid: Decimal = ZERO  # ALL fees ever paid on this stock (buy + sell, all-time)
    lifetime_buy_quantity: Decimal = ZERO
    transactions_considered: int = 0
    as_of_error: str | None = None  # set if the ledger is inconsistent (should be unreachable in practice)

    @property
    def cost_basis(self) -> Decimal:
        return self.purchase_cost + self.fees_in_basis

    @property
    def average_price(self) -> Decimal | None:
        if self.quantity <= ZERO:
            return None
        return (self.purchase_cost / self.quantity).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    @property
    def is_open(self) -> bool:
        return self.quantity > ZERO


def _replay_transactions(stock: Stock, txns: list[PortfolioTransaction]) -> HoldingCalc:
    """Pure function: given one stock's transactions in chronological
    order, replay BUY/SELL fills and return the resulting weighted-average
    cost-basis state. `txns` must already be filtered to the stock/date
    window the caller cares about (see compute_holdings' as_of handling)."""
    calc = HoldingCalc(stock=stock)
    for txn in txns:
        qty = txn.quantity
        price = txn.price_per_share
        fees = txn.fees
        calc.transactions_considered += 1
        if txn.transaction_type == TransactionType.BUY:
            calc.purchase_cost += qty * price
            calc.fees_in_basis += fees
            calc.quantity += qty
            calc.total_fees_paid += fees
            calc.lifetime_buy_quantity += qty
        else:  # SELL
            if qty > calc.quantity:
                # Should be unreachable — every write path validates
                # against the same replay before committing. Surfacing
                # this rather than silently clamping means a data bug
                # shows up loudly instead of quietly misreporting P/L.
                calc.as_of_error = (
                    f"Sell of {qty} exceeds held quantity {calc.quantity} for "
                    f"{stock.exchange}:{stock.trading_code} as of {txn.transaction_date} "
                    f"(transaction id={txn.id})"
                )
                continue
            ratio = (qty / calc.quantity) if calc.quantity > ZERO else ZERO
            cost_removed_purchase = calc.purchase_cost * ratio
            cost_removed_fees = calc.fees_in_basis * ratio
            proceeds = qty * price - fees
            calc.realized_pl += proceeds - (cost_removed_purchase + cost_removed_fees)
            calc.purchase_cost -= cost_removed_purchase
            calc.fees_in_basis -= cost_removed_fees
            calc.quantity -= qty
            calc.total_fees_paid += fees
    return calc


def _fetch_transactions(portfolio: Portfolio, as_of: date | None, stock: Stock | None = None):
    """One query for everything, chronologically ordered, date-filtered
    server-side so a future-dated transaction is invisible to "current
    state" math (it still exists in the ledger — see the transactions
    history page — it just hasn't taken effect yet)."""
    as_of = as_of or timezone.localdate()
    qs = portfolio.transactions.select_related("stock").filter(transaction_date__lte=as_of)
    if stock is not None:
        qs = qs.filter(stock=stock)
    return list(qs.order_by("transaction_date", "created_at", "id"))


def compute_holding(portfolio: Portfolio, stock: Stock, as_of: date | None = None) -> HoldingCalc:
    txns = _fetch_transactions(portfolio, as_of, stock=stock)
    return _replay_transactions(stock, txns)


def compute_holdings(portfolio: Portfolio, as_of: date | None = None, include_closed: bool = False) -> list[HoldingCalc]:
    """All stocks this portfolio has ever transacted in, replayed in one
    pass (one query, grouped in Python) rather than one query per stock —
    the N+1 a naive per-stock loop would otherwise cause."""
    txns = _fetch_transactions(portfolio, as_of)
    by_stock: dict[int, list[PortfolioTransaction]] = {}
    stocks_by_id: dict[int, Stock] = {}
    for txn in txns:
        by_stock.setdefault(txn.stock_id, []).append(txn)
        stocks_by_id[txn.stock_id] = txn.stock

    holdings = []
    for stock_id, stock_txns in by_stock.items():
        calc = _replay_transactions(stocks_by_id[stock_id], stock_txns)
        if calc.is_open or include_closed:
            holdings.append(calc)
    holdings.sort(key=lambda h: h.stock.trading_code)
    return holdings


# ---------------------------------------------------------------------------
# Presentation-ready holding row (adds live price / P&L / quote status)
# ---------------------------------------------------------------------------


def holding_row(calc: HoldingCalc, now=None, recent_bars=None) -> dict:
    stock = calc.stock
    q = quote_status(stock, now=now, recent_bars=recent_bars)
    price = q["price"]

    market_value = quantize_money(price * calc.quantity) if price is not None else None
    cost_basis = quantize_money(calc.cost_basis)
    unrealized_pl = quantize_money(market_value - cost_basis) if market_value is not None else None
    unrealized_pl_pct = None
    if unrealized_pl is not None and calc.cost_basis > ZERO:
        unrealized_pl_pct = ((market_value - cost_basis) / calc.cost_basis * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    today = (now or timezone.now()).date() if now else timezone.localdate()
    prev_close = _previous_close(stock, today, recent_bars=recent_bars)
    today_pl = today_pl_pct = None
    if price is not None and prev_close is not None and prev_close > ZERO and calc.quantity > ZERO:
        per_share = price - prev_close
        today_pl = quantize_money(per_share * calc.quantity)
        today_pl_pct = (per_share / prev_close * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return {
        "stock": stock,
        "exchange": stock.exchange,
        "trading_code": stock.trading_code,
        "company_name": stock.company_name,
        "sector": stock.sector,
        "quantity": calc.quantity,
        "average_price": calc.average_price,
        "purchase_cost": quantize_money(calc.purchase_cost),
        "fees_in_basis": quantize_money(calc.fees_in_basis),
        "cost_basis": cost_basis,
        "latest_price": price,
        "market_value": market_value,
        "unrealized_pl": unrealized_pl,
        "unrealized_pl_pct": unrealized_pl_pct,
        "today_pl": today_pl,
        "today_pl_pct": today_pl_pct,
        "realized_pl": quantize_money(calc.realized_pl),
        "quote_status": q["status"],
        "quote_label": q["label"],
        "quote_as_of": q["as_of"],
        "data_warning": calc.as_of_error,
    }


# ---------------------------------------------------------------------------
# Portfolio-level summary
# ---------------------------------------------------------------------------


def portfolio_summary(portfolio: Portfolio, as_of: date | None = None, now=None) -> dict:
    now = now or timezone.now()
    # One replay covers both open holdings and fully-closed positions
    # (needed for all-time realized P/L) — avoids querying the
    # transaction ledger twice for the same portfolio.
    all_calcs = compute_holdings(portfolio, as_of=as_of, include_closed=True)
    open_calcs = [c for c in all_calcs if c.is_open]

    today = (now or timezone.now()).date() if now else timezone.localdate()
    recent_bars = bulk_recent_bars([c.stock for c in open_calcs], today)
    rows = [holding_row(c, now=now, recent_bars=recent_bars) for c in open_calcs]

    total_cost_basis = sum((r["cost_basis"] for r in rows), ZERO)
    total_market_value = sum((r["market_value"] for r in rows if r["market_value"] is not None), ZERO)
    priced_rows = [r for r in rows if r["market_value"] is not None]
    total_unrealized_pl = quantize_money(total_market_value - sum((r["cost_basis"] for r in priced_rows), ZERO)) if priced_rows else None
    total_unrealized_pl_pct = None
    priced_cost_basis = sum((r["cost_basis"] for r in priced_rows), ZERO)
    if priced_rows and priced_cost_basis > ZERO:
        total_unrealized_pl_pct = (total_unrealized_pl / priced_cost_basis * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    total_realized_pl = quantize_money(sum((c.realized_pl for c in all_calcs), ZERO))
    today_total = [r["today_pl"] for r in rows if r["today_pl"] is not None]
    today_total_pl = quantize_money(sum(today_total, ZERO)) if today_total else None

    for r in rows:
        r["allocation_pct"] = (
            (r["market_value"] / total_market_value * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if r["market_value"] is not None and total_market_value > ZERO
            else None
        )

    ranked = [r for r in rows if r["unrealized_pl_pct"] is not None]
    best = max(ranked, key=lambda r: r["unrealized_pl_pct"]) if ranked else None
    worst = min(ranked, key=lambda r: r["unrealized_pl_pct"]) if ranked else None

    allocation_by_exchange: dict[str, Decimal] = {}
    allocation_by_sector: dict[str, Decimal] = {}
    for r in rows:
        if r["market_value"] is None:
            continue
        allocation_by_exchange[r["exchange"]] = allocation_by_exchange.get(r["exchange"], ZERO) + r["market_value"]
        sector = r["sector"] or "Unclassified"
        allocation_by_sector[sector] = allocation_by_sector.get(sector, ZERO) + r["market_value"]

    def _as_pct_breakdown(totals: dict[str, Decimal]) -> list[dict]:
        out = [
            {
                "label": k,
                "value": quantize_money(v),
                "pct": (v / total_market_value * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if total_market_value > ZERO else None,
            }
            for k, v in totals.items()
        ]
        out.sort(key=lambda x: x["value"], reverse=True)
        return out

    return {
        "portfolio": portfolio,
        "holdings": rows,
        "open_holdings_count": len(rows),
        "total_cost_basis": quantize_money(total_cost_basis),
        "total_market_value": quantize_money(total_market_value) if rows else quantize_money(ZERO),
        "total_unrealized_pl": total_unrealized_pl,
        "total_unrealized_pl_pct": total_unrealized_pl_pct,
        "total_realized_pl": total_realized_pl,
        "today_total_pl": today_total_pl,
        "best_holding": best,
        "worst_holding": worst,
        "allocation_by_stock": [
            {"label": r["trading_code"], "exchange": r["exchange"], "value": r["market_value"], "pct": r["allocation_pct"]}
            for r in rows
            if r["market_value"] is not None
        ],
        "allocation_by_exchange": _as_pct_breakdown(allocation_by_exchange),
        "allocation_by_sector": _as_pct_breakdown(allocation_by_sector),
        "has_any_data_warning": any(r["data_warning"] for r in rows),
    }


# ---------------------------------------------------------------------------
# Validation + mutation
# ---------------------------------------------------------------------------


def validate_transaction(
    portfolio: Portfolio,
    stock: Stock,
    transaction_type: str,
    quantity: Decimal,
    price_per_share: Decimal,
    fees: Decimal,
    transaction_date: date,
    exclude_transaction_id: int | None = None,
) -> None:
    """Raises PortfolioValidationError with a user-facing message on any
    invalid input. Called before both create and update, and re-run
    against the full post-edit ledger so an edit can't retroactively
    create a negative holding (see module docstring)."""
    if transaction_type not in (TransactionType.BUY, TransactionType.SELL):
        raise PortfolioValidationError("Transaction type must be BUY or SELL.")
    if quantity is None or quantity <= ZERO:
        raise PortfolioValidationError("Quantity must be greater than zero.")
    if price_per_share is None or price_per_share < ZERO:
        raise PortfolioValidationError("Price per share cannot be negative.")
    if fees is None or fees < ZERO:
        raise PortfolioValidationError("Fees/charges cannot be negative.")
    if transaction_date is None:
        raise PortfolioValidationError("Transaction date is required.")

    if transaction_type == TransactionType.SELL:
        # Replay every transaction for this (portfolio, stock) up to and
        # including this date, as if this SELL were already applied, to
        # confirm it never drives the running quantity negative at any
        # point in the sequence — not just at the end.
        qs = portfolio.transactions.filter(stock=stock, transaction_date__lte=transaction_date)
        if exclude_transaction_id:
            qs = qs.exclude(id=exclude_transaction_id)
        existing = list(qs.order_by("transaction_date", "created_at", "id"))
        held_at_date = _replay_transactions(stock, existing).quantity
        if quantity > held_at_date:
            raise PortfolioValidationError(
                f"Cannot sell {quantity} shares — only {held_at_date} held as of {transaction_date}."
            )


def validate_ledger_after_mutation(portfolio: Portfolio, stock: Stock) -> None:
    """Re-validates the *entire* chronological ledger for this
    (portfolio, stock), not just up to the mutated row's date — editing
    an early BUY's quantity down can invalidate a much later SELL. Raises
    on the first inconsistency found."""
    all_txns = list(
        portfolio.transactions.filter(stock=stock).order_by("transaction_date", "created_at", "id")
    )
    calc = _replay_transactions(stock, all_txns)
    if calc.as_of_error:
        raise PortfolioValidationError(calc.as_of_error)


@transaction.atomic
def create_transaction(
    portfolio: Portfolio,
    stock: Stock,
    transaction_type: str,
    quantity: Decimal,
    price_per_share: Decimal,
    fees: Decimal,
    transaction_date: date,
    notes: str = "",
) -> PortfolioTransaction:
    fees = fees if fees is not None else ZERO
    validate_transaction(portfolio, stock, transaction_type, quantity, price_per_share, fees, transaction_date)
    return PortfolioTransaction.objects.create(
        portfolio=portfolio,
        stock=stock,
        transaction_type=transaction_type,
        quantity=quantity,
        price_per_share=price_per_share,
        fees=fees,
        transaction_date=transaction_date,
        notes=notes or "",
    )


@transaction.atomic
def update_transaction(
    txn: PortfolioTransaction,
    transaction_type: str,
    quantity: Decimal,
    price_per_share: Decimal,
    fees: Decimal,
    transaction_date: date,
    notes: str = "",
) -> PortfolioTransaction:
    fees = fees if fees is not None else ZERO
    validate_transaction(
        txn.portfolio, txn.stock, transaction_type, quantity, price_per_share, fees, transaction_date,
        exclude_transaction_id=txn.id,
    )
    txn.transaction_type = transaction_type
    txn.quantity = quantity
    txn.price_per_share = price_per_share
    txn.fees = fees
    txn.transaction_date = transaction_date
    txn.notes = notes or ""
    txn.save()
    # An edit to an early transaction can invalidate a later one even
    # though validate_transaction (above) only checked up to *this* row's
    # date — confirm the whole post-edit sequence still holds.
    validate_ledger_after_mutation(txn.portfolio, txn.stock)
    return txn


@transaction.atomic
def delete_transaction(txn: PortfolioTransaction) -> None:
    portfolio, stock = txn.portfolio, txn.stock
    txn.delete()
    validate_ledger_after_mutation(portfolio, stock)
