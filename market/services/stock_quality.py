"""Small, read-only data-quality assessment used by the Stocks page.

This does not change model training or hide shares.  It simply separates
shares whose recent trading data is too thin to present model output as
equally trustworthy.
"""
from __future__ import annotations

from datetime import timedelta
from math import ceil

from django.db.models import Avg, Count, Max

from market.models import PriceHistory

RECENT_CALENDAR_DAYS = 60
MIN_HISTORY_DAYS = 80
MIN_RECENT_COVERAGE = 0.80
LOW_LIQUIDITY_PERCENTILE = 0.10


def assess_stock_quality(stocks) -> dict[int, dict]:
    """Return plain-language data-quality labels keyed by stock ID.

    A share is labelled limited when it lacks enough history, misses at least
    20% of recent exchange sessions, or its recent average traded value is in
    the bottom 10% of its full active exchange universe. The latter is
    deliberately independent of a page search/filter so a share never
    changes category merely because it is viewed on its own.
    """
    stock_list = list(stocks)
    if not stock_list:
        return {}

    result = {
        stock.id: {"limited": False, "reasons": [], "recent_sessions": 0, "history_days": 0, "avg_value": None}
        for stock in stock_list
    }
    by_exchange: dict[str, list[int]] = {}
    for stock in stock_list:
        by_exchange.setdefault(stock.exchange, []).append(stock.id)

    for exchange, stock_ids in by_exchange.items():
        latest = PriceHistory.objects.live().filter(stock__exchange=exchange).aggregate(latest=Max("date"))["latest"]
        if latest is None:
            for stock_id in stock_ids:
                result[stock_id]["limited"] = True
                result[stock_id]["reasons"] = ["No price history is available yet"]
            continue

        cutoff = latest - timedelta(days=RECENT_CALENDAR_DAYS)
        expected_sessions = PriceHistory.objects.live().filter(stock__exchange=exchange, date__gte=cutoff).values("date").distinct().count()
        recent = {
            row["stock_id"]: row
            for row in (
                PriceHistory.objects.live()
                .filter(stock__exchange=exchange, stock__is_active=True, date__gte=cutoff)
                .values("stock_id")
                .annotate(recent_sessions=Count("date", distinct=True), avg_value=Avg("value"))
            )
        }
        history = {
            row["stock_id"]: row["history_days"]
            for row in (
                PriceHistory.objects.live()
                .filter(stock_id__in=stock_ids)
                .values("stock_id")
                .annotate(history_days=Count("date", distinct=True))
            )
        }
        values = sorted(float(row["avg_value"] or 0) for row in recent.values())
        liquidity_cutoff = values[max(0, ceil(len(values) * LOW_LIQUIDITY_PERCENTILE) - 1)] if len(values) >= 10 else None
        minimum_sessions = ceil(expected_sessions * MIN_RECENT_COVERAGE)

        for stock_id in stock_ids:
            stats = recent.get(stock_id, {})
            entry = result[stock_id]
            entry["recent_sessions"] = int(stats.get("recent_sessions") or 0)
            entry["history_days"] = int(history.get(stock_id) or 0)
            entry["avg_value"] = None if stats.get("avg_value") is None else float(stats["avg_value"])
            reasons = []
            if entry["history_days"] < MIN_HISTORY_DAYS:
                reasons.append("Too little price history")
            if entry["recent_sessions"] < minimum_sessions:
                reasons.append("Not traded regularly in the last 60 days")
            if liquidity_cutoff is not None and (entry["avg_value"] or 0) <= liquidity_cutoff:
                reasons.append("Recent traded value is in the lowest 10%")
            entry["limited"] = bool(reasons)
            entry["reasons"] = reasons
    return result
