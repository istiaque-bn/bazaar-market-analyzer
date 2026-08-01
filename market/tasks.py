"""Named, scheduled Celery tasks for all Bazaar background work.

Every market-writing task:
  - acquires market.services.autosync.exclusive_db_write (thread lock +
    cross-process Redis lock) so duplicate/concurrent workers serialize
    instead of racing the SQLite DB;
  - retries only transient failures (lock busy, DB "locked", network) with
    backoff, up to a bounded number of attempts;
  - is bounded by a hard/soft time limit;
  - records a market.models.TaskRun success/failure row via
    @record_task_run, independent of Celery's own result backend;
  - is idempotent: fetch/analysis/append use update_or_create / merge
    upserts, so re-running (a retry, or a duplicate trigger) recomputes
    the same rows rather than duplicating data.
"""
from __future__ import annotations

import requests
from celery import shared_task
from django.db.utils import OperationalError

from market.services.task_status import record_task_run

_TRANSIENT_ERRORS = (TimeoutError, OperationalError, requests.exceptions.RequestException)


@shared_task(
    name="market.tasks.sync_live_market",
    autoretry_for=_TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=2,
    time_limit=90,
    soft_time_limit=75,
)
@record_task_run("market.tasks.sync_live_market")
def sync_live_market():
    """Live DSE/CSE quote sync. Runs every tick (see beat schedule) but is a
    no-op unless AUTO_SYNC_INTERVAL_* has actually elapsed — maybe_sync()
    keeps the existing market-hours-aware cadence without needing separate
    beat entries per session state."""
    from market.services.autosync import maybe_sync

    return maybe_sync(force=False)


@shared_task(
    name="market.tasks.fetch_all_market_data",
    autoretry_for=_TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=3,
    time_limit=600,
    soft_time_limit=540,
)
@record_task_run("market.tasks.fetch_all_market_data")
def fetch_all_market_data(include_history: bool = False):
    from market.services.analyzer import fetch_all
    from market.services.autosync import exclusive_db_write

    with exclusive_db_write(blocking=True, timeout=180):
        return fetch_all(use_demo_if_empty=False, include_history=include_history)


@shared_task(
    name="market.tasks.run_full_analysis",
    autoretry_for=_TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=3,
    time_limit=900,
    soft_time_limit=840,
)
@record_task_run("market.tasks.run_full_analysis")
def run_full_analysis_task(train_ml: bool = True):
    from market.services.analyzer import run_full_analysis
    from market.services.autosync import exclusive_db_write

    with exclusive_db_write(blocking=True, timeout=600):
        return run_full_analysis(train_ml=train_ml)


@shared_task(
    name="market.tasks.seed_demo_and_analyze",
    autoretry_for=_TRANSIENT_ERRORS,
    retry_backoff=True,
    max_retries=2,
    time_limit=300,
    soft_time_limit=270,
)
@record_task_run("market.tasks.seed_demo_and_analyze")
def seed_demo_and_analyze():
    from market.services.analyzer import run_full_analysis
    from market.services.autosync import exclusive_db_write
    from market.services.dse_fetcher import seed_demo_universe

    with exclusive_db_write(blocking=True, timeout=180):
        seeded = seed_demo_universe()
        analysis = run_full_analysis(train_ml=True)
    return {"seeded": seeded, "analysis": analysis}


@shared_task(
    name="market.tasks.append_daily_bars",
    autoretry_for=_TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=3,
    time_limit=600,
    soft_time_limit=540,
)
@record_task_run("market.tasks.append_daily_bars")
def append_daily_bars():
    """Scheduled job: append/update today's OHLC at 10:05 and 14:05 and,
    if AUTO_ANALYZE_AFTER_APPEND, re-run analysis (locking is inside
    run_scheduled_append via exclusive_db_write)."""
    from market.services.daily_append import run_scheduled_append

    return run_scheduled_append()


@shared_task(
    name="market.tasks.train_ml_model",
    autoretry_for=_TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=120,
    max_retries=2,
    time_limit=600,
    soft_time_limit=540,
)
@record_task_run("market.tasks.train_ml_model")
def train_ml_model():
    """Standalone forward-return model retrain, independent of the
    analysis pass (which can also optionally retrain inline)."""
    from market.services.autosync import exclusive_db_write
    from market.services.ml_model import train_model

    with exclusive_db_write(blocking=True, timeout=300):
        return train_model()


@shared_task(
    name="market.tasks.close_learn_settlement",
    autoretry_for=_TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=120,
    max_retries=2,
    time_limit=600,
    soft_time_limit=540,
)
@record_task_run("market.tasks.close_learn_settlement")
def close_learn_settlement():
    """Settle due next-day-close forecasts, retrain the close-learn model
    if anything settled, and generate the next round of forecasts."""
    from django.utils import timezone

    from market.services.autosync import exclusive_db_write
    from market.services.close_learn import run_close_learn_cycle

    with exclusive_db_write(blocking=True, timeout=300):
        return run_close_learn_cycle(as_of=timezone.localdate(), train=True)


@shared_task(
    name="market.tasks.sync_holiday_calendar",
    autoretry_for=_TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=2,
    time_limit=60,
    soft_time_limit=45,
)
@record_task_run("market.tasks.sync_holiday_calendar")
def sync_holiday_calendar_task():
    """Refresh market.models.MarketHoliday from DSE's published holiday
    notice. Scheduled to fire on the 28th-31st of each month (see beat
    schedule) but only actually runs on the true last day of the month —
    Celery's crontab has no "last day of month" primitive, so this
    self-filters the earlier over-fires."""
    from datetime import timedelta

    from django.utils import timezone

    from market.services.autosync import exclusive_db_write
    from market.services.holiday_sync import sync_holiday_calendar

    today = timezone.localdate()
    if (today + timedelta(days=1)).day != 1:
        return {"ok": True, "skipped": "not the last day of the month"}

    with exclusive_db_write(blocking=True, timeout=60):
        return sync_holiday_calendar()


@shared_task(
    name="market.tasks.analyze_and_notify",
    time_limit=900,
    soft_time_limit=840,
)
@record_task_run("market.tasks.analyze_and_notify")
def analyze_and_notify():
    result = run_full_analysis_task(train_ml=True)
    from notifications.tasks import send_daily_digest

    send_daily_digest.delay()
    return result
