"""Lightweight intraday analysis (Hybrid automation — see market/tasks.py's
run_intraday_analysis and config/celery.py's beat schedule).

Deliberately NOT a second scoring/prediction pipeline: it refreshes only
market.models.TechnicalSnapshot (moving averages, RSI, MACD, Bollinger,
ATR — a single pass over already-fetched PriceHistory rows, no network
call) for stocks with newer data than their last snapshot. It never
writes a new AnalysisResult row, never runs market.services.predictor's
historical-analogue search, never blends an ML probability, and never
retrains anything — that's all still exclusively market.services.
analyzer.run_full_analysis's job, run once per session (see
market.tasks.run_full_analysis_task / run_end_of_day_pipeline).

This keeps "new quote arrived -> something visibly updates" honest
without creating a new near-duplicate AnalysisResult row on every small
price tick, and without repeating the expensive year-long analogue scan
intraday.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.utils import timezone

from market.models import Stock, TaskRun, TaskStatus, TechnicalSnapshot
from market.services.autosync import is_market_hours
from market.services.indicators import latest_indicator_row, prices_to_df

logger = logging.getLogger(__name__)

TASK_NAME = "market.tasks.run_intraday_analysis"

_SNAPSHOT_FIELDS = (
    "sma_20",
    "sma_50",
    "sma_200",
    "ema_12",
    "ema_26",
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_hist",
    "bb_upper",
    "bb_middle",
    "bb_lower",
    "atr_14",
    "volume_sma_20",
)


def intraday_interval_seconds() -> int:
    return int(getattr(settings, "AUTO_INTRADAY_ANALYSIS_INTERVAL", 900))


def _last_run_at():
    """Last successful run's start time, from the durable TaskRun table —
    not in-memory state, so this stays correct across worker restarts and
    multiple worker processes (unlike a per-process dict)."""
    run = TaskRun.objects.filter(task_name=TASK_NAME, status=TaskStatus.SUCCESS).order_by("-started_at").first()
    return run.started_at if run else None


def _has_new_data_since(since, enabled) -> bool:
    qs = Stock.objects.filter(is_active=True, exchange__in=enabled)
    if since is not None:
        qs = qs.filter(updated_at__gte=since)
    return qs.exists()


def run_intraday_analysis_unlocked() -> dict:
    """Does the actual work — caller (maybe_run_intraday_analysis) holds
    the lock and has already checked eligibility."""
    from market.services.exchange_config import enabled_exchanges

    enabled = enabled_exchanges()
    updated = 0
    skipped_no_data = 0
    for stock in Stock.objects.filter(is_active=True, exchange__in=enabled):
        # .live() excludes synthetic/demo/mirror-fallback rows — the same
        # "no fabricated data in production analysis" rule the full
        # pipeline follows (see market.models.PriceHistoryQuerySet.live).
        df = prices_to_df(stock.prices.live())
        if df.empty:
            skipped_no_data += 1
            continue
        ind = latest_indicator_row(df)
        if not ind:
            skipped_no_data += 1
            continue
        as_of_raw = df.iloc[-1]["date"]
        as_of = as_of_raw.date() if hasattr(as_of_raw, "date") else as_of_raw
        TechnicalSnapshot.objects.update_or_create(
            stock=stock,
            as_of=as_of,
            defaults={field: ind.get(field) for field in _SNAPSHOT_FIELDS},
        )
        updated += 1
    return {"ok": True, "updated": updated, "skipped_no_data": skipped_no_data, "enabled_exchanges": enabled}


def maybe_run_intraday_analysis(force: bool = False) -> dict:
    """Eligibility gate, called every beat tick (like autosync.maybe_sync)
    — only does real work when all of these hold:
      - AUTO_INTRADAY_ANALYSIS is enabled
      - the market is open (skip is not a failure — see market.tasks)
      - the minimum interval has elapsed since the last successful run
      - there's newer Stock data than that last run (nothing to refresh
        otherwise — avoids identical snapshot rows on every tick)
      - no equivalent run is already in flight (non-blocking lock)
    """
    from market.services.exchange_config import enabled_exchanges
    from market.services.locking import LockBusy, distributed_lock

    if not getattr(settings, "AUTO_INTRADAY_ANALYSIS", True) and not force:
        return {"ok": True, "skipped": "disabled"}

    if not force and not is_market_hours():
        return {"ok": True, "skipped": "market_closed"}

    enabled = enabled_exchanges()
    if not enabled:
        return {"ok": True, "skipped": "no_enabled_exchanges"}

    last_run = _last_run_at()
    interval = intraday_interval_seconds()
    if not force and last_run is not None:
        age = (timezone.now() - last_run).total_seconds()
        if age < interval:
            return {"ok": True, "skipped": "interval_not_elapsed", "age_seconds": age}

    if not force and not _has_new_data_since(last_run, enabled):
        return {"ok": True, "skipped": "no_new_data"}

    try:
        with distributed_lock("intraday-analysis", timeout=120, blocking_timeout=0):
            return run_intraday_analysis_unlocked()
    except LockBusy:
        return {"ok": True, "skipped": "already_running"}
