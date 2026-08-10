"""Read-only safety helpers for next-close research experiments.

These helpers are intentionally not wired into model promotion. They provide
one consistent, chronological holdout boundary and data-quality gates before
any challenger is allowed to be evaluated.
"""
from __future__ import annotations

import pandas as pd
from django.db.models import Count, Q

from market.models import Exchange, PriceHistory, Stock

FINAL_HOLDOUT_CALENDAR_DAYS = 90
MIN_SECTOR_COVERAGE = 0.90


def split_final_holdout(panel: pd.DataFrame, *, calendar_days: int = FINAL_HOLDOUT_CALENDAR_DAYS) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split a date-sorted panel into research data and untouched final data."""
    if panel.empty or "date" not in panel:
        return panel.copy(), panel.copy()
    data = panel.copy()
    data["date"] = pd.to_datetime(data["date"])
    cutoff = data["date"].max() - pd.Timedelta(days=calendar_days)
    research = data[data["date"] < cutoff].copy()
    holdout = data[data["date"] >= cutoff].copy()
    return research, holdout


def sector_data_is_usable(exchange: str = Exchange.DSE) -> bool:
    active = Stock.objects.filter(exchange=exchange, is_active=True)
    total = active.count()
    populated = active.exclude(sector="").count()
    return bool(total and populated / total >= MIN_SECTOR_COVERAGE)


def next_close_data_quality_summary(exchange: str = Exchange.DSE) -> dict:
    """A no-write preflight summary for next-close research/training."""
    stocks = Stock.objects.filter(exchange=exchange, is_active=True)
    total = stocks.count()
    blank_sectors = stocks.filter(sector="").count()
    prices = PriceHistory.objects.live().filter(stock__exchange=exchange)
    return {
        "exchange": exchange,
        "active_stocks": total,
        "blank_sector_count": blank_sectors,
        "sector_coverage_pct": round((total - blank_sectors) / total * 100, 2) if total else 0.0,
        "sector_features_usable": sector_data_is_usable(exchange),
        "price_rows": prices.count(),
        "price_rows_with_quality_flags": prices.exclude(quality_flags=[]).count(),
        "unknown_provenance_rows": prices.filter(Q(source="unknown") | Q(adjustment_status="unknown")).count(),
    }
