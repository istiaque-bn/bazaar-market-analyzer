"""
DSE / CSE trading-session helpers (Asia/Dhaka).

Regular session (both exchanges, typical schedule):
  Sunday–Thursday, 10:00 – 14:30
"""
from __future__ import annotations

from datetime import datetime, timedelta, time as dtime, date

from django.utils import timezone

# Python weekday: Mon=0 … Sun=6 — BD boards trade Sun–Thu
TRADING_WEEKDAYS = (6, 0, 1, 2, 3)

SESSION_OPEN = dtime(10, 0)
SESSION_CLOSE = dtime(14, 30)


def _local_now():
    return timezone.localtime()


def _is_trading_day(dt) -> bool:
    return dt.weekday() in TRADING_WEEKDAYS


def _at_on_date(d: date, t: dtime):
    return timezone.make_aware(datetime.combine(d, t))


def _next_trading_date(from_date: date) -> date:
    d = from_date
    for _ in range(8):
        d = d + timedelta(days=1)
        if d.weekday() in TRADING_WEEKDAYS:
            return d
    return from_date + timedelta(days=2)


def _format_when(now_local: datetime, next_at: datetime) -> str:
    """Prefer today/tomorrow based on Asia/Dhaka calendar dates."""
    next_local = timezone.localtime(next_at)
    today = now_local.date()
    target = next_local.date()
    clock = next_local.strftime("%I:%M %p").lstrip("0")
    if target == today:
        return f"today {clock}"
    if target == today + timedelta(days=1):
        return f"tomorrow {clock}"
    return next_local.strftime("%a %d %b, ") + clock


def session_status(exchange: str = "DSE", now=None) -> dict:
    """
    Return open/closed status and next open/close for an exchange.
    DSE and CSE use the same regular board hours in this app.
    """
    now = timezone.localtime(now) if now else _local_now()
    exchange = (exchange or "DSE").upper()
    today = now.date()
    open_today = _at_on_date(today, SESSION_OPEN)
    close_today = _at_on_date(today, SESSION_CLOSE)
    trading_today = today.weekday() in TRADING_WEEKDAYS

    if trading_today and open_today <= now < close_today:
        is_open = True
        next_event = "closes"
        next_at = close_today
        label = "Open"
    else:
        is_open = False
        label = "Closed"
        next_event = "opens"
        if trading_today and now < open_today:
            # Early morning on a trading day → opens later TODAY (not tomorrow)
            next_at = open_today
        elif trading_today and now >= close_today:
            # After close → next trading session (often tomorrow)
            next_at = _at_on_date(_next_trading_date(today), SESSION_OPEN)
        else:
            # Fri/Sat (or non-trading) → next Sun/Mon/…
            next_at = _at_on_date(_next_trading_date(today), SESSION_OPEN)

    next_when = _format_when(now, next_at)
    return {
        "exchange": exchange,
        "is_open": is_open,
        "status": label,
        "session": f"{SESSION_OPEN.strftime('%H:%M')}–{SESSION_CLOSE.strftime('%H:%M')}",
        "next_event": next_event,
        "next_at": next_at.isoformat(),
        "next_when": next_when,
        "message": (
            f"{label} · closes {next_when}" if is_open else f"{label} · opens {next_when}"
        ),
    }


def both_exchanges_status(now=None) -> dict:
    now = timezone.localtime(now) if now else _local_now()
    return {
        "dse": session_status("DSE", now),
        "cse": session_status("CSE", now),
        "as_of": now.isoformat(),
    }
