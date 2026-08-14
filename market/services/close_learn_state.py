"""State and bias helpers for the next-close learning cycle.

They are re-exported by ``close_learn`` so callers retain the historical
import path while the learner's state concerns remain independently readable.
"""
from datetime import date, timedelta

from django.db.models import Avg
from django.utils import timezone

from market.models import CloseLearnState, PriceHistory, Stock
from market.services.market_hours import TRADING_WEEKDAYS

LIQUID_TOP_N = 80
STOCK_BIAS_MIN_SETTLES = 8


def next_trading_day(from_date: date) -> date:
    from market.services.trading_calendar import closure_reason

    current = from_date
    for _ in range(15):
        current += timedelta(days=1)
        if current.weekday() in TRADING_WEEKDAYS and closure_reason(current) is None:
            return current
    return from_date + timedelta(days=2)


def get_learn_state(key: str = "global") -> CloseLearnState:
    state, _ = CloseLearnState.objects.get_or_create(key=key)
    return state


def stock_bias_key(stock_id: int) -> str:
    return f"stock:{stock_id}"


def liquid_stock_ids(limit: int = LIQUID_TOP_N, *, as_of: date | None = None) -> set[int]:
    as_of = as_of or timezone.localdate()
    rows = (
        PriceHistory.objects.live()
        .filter(date__gte=as_of - timedelta(days=45), date__lte=as_of)
        .values("stock_id")
        .annotate(avg_vol=Avg("volume"))
        .order_by("-avg_vol")[:limit]
    )
    return {row["stock_id"] for row in rows}


def get_combined_bias(stock: Stock | None, liquid_ids: set[int] | None = None) -> tuple[float, float, float]:
    global_state = get_learn_state("global")
    global_bias = float(global_state.return_bias or 0.0)
    stock_bias = 0.0
    if stock is not None:
        ids = liquid_ids if liquid_ids is not None else liquid_stock_ids()
        if stock.id in ids:
            state = get_learn_state(stock_bias_key(stock.id))
            if state.settled_count >= STOCK_BIAS_MIN_SETTLES:
                stock_bias = float(state.return_bias or 0.0)
    return global_bias + stock_bias, global_bias, stock_bias
