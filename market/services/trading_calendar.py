"""
Answers "was the market closed on this date, and why" for display purposes
(e.g. labeling a missing row in a price-history table as "Weekend" or a
named holiday instead of leaving it blank). Backed by market_hours'
TRADING_WEEKDAYS for the regular Sun-Thu week, plus the hand-maintained
MarketHoliday table for everything else (Eid, national holidays, ...).
"""
from __future__ import annotations

from datetime import date as date_cls

from .market_hours import TRADING_WEEKDAYS


def is_weekend(d: date_cls) -> bool:
    return d.weekday() not in TRADING_WEEKDAYS


def closure_reason(d: date_cls) -> str | None:
    """Why the market was closed on `d`, or None if it was a regular trading
    day. Weekend is checked first since it needs no query; a holiday that
    happens to fall on Fri/Sat (rare, but possible) is reported as "Weekend"."""
    if is_weekend(d):
        return "Weekend"
    from market.models import MarketHoliday

    return MarketHoliday.objects.filter(date=d).values_list("name", flat=True).first()
