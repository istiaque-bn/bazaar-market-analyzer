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
from celery import chain, shared_task
from django.db.utils import OperationalError

from market.services.task_status import record_task_run

_TRANSIENT_ERRORS = (TimeoutError, OperationalError, requests.exceptions.RequestException)
HISTORY_BATCH_SIZE = 20


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
    name="market.tasks.run_intraday_analysis",
    autoretry_for=_TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=2,
    time_limit=120,
    soft_time_limit=100,
)
@record_task_run("market.tasks.run_intraday_analysis")
def run_intraday_analysis():
    """Lightweight technical-snapshot refresh — see market.services.
    intraday_analysis's module docstring for why this is deliberately not
    a second full-analysis pipeline. Runs every beat tick (see beat
    schedule) but self-throttles exactly like sync_live_market does."""
    from market.services.intraday_analysis import maybe_run_intraday_analysis

    return maybe_run_intraday_analysis(force=False)


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
    from market.services.locking import LockBusy, distributed_lock

    try:
        # A full fetch is expensive and must never queue behind another full
        # fetch.  Waiting consumed the task's time budget and produced stale
        # STARTED rows when the worker was later killed at its hard limit.
        with distributed_lock("full-market-fetch", timeout=660, blocking_timeout=0):
            with exclusive_db_write(blocking=True, timeout=180):
                # Live quotes are one bounded bulk operation. Historical data
                # is per-symbol I/O; doing all ~395 symbols under this task's
                # 540s soft limit was the direct cause of the production alert.
                live = fetch_all(use_demo_if_empty=False, include_history=False)
            if not include_history:
                return live

            from market.models import Stock
            from market.services.exchange_config import enabled_exchanges

            signatures = []
            for exchange in enabled_exchanges():
                codes = list(Stock.objects.filter(exchange=exchange, is_active=True).values_list("trading_code", flat=True))
                for offset in range(0, len(codes), HISTORY_BATCH_SIZE):
                    signatures.append(fetch_market_history_batch.si(exchange, codes[offset : offset + HISTORY_BATCH_SIZE]))
            if signatures:
                # Serial batches preserve the existing single-writer safety;
                # the analysis callback is reached only after every batch has
                # completed successfully or partially (bad symbols isolated).
                chain(*signatures, run_full_analysis_task.si(train_ml=True)).delay()
            return live | {"history_batches": len(signatures), "history_batch_size": HISTORY_BATCH_SIZE, "history_queued": True}
    except LockBusy:
        return {"ok": True, "skipped": "already_running"}


@shared_task(
    name="market.tasks.fetch_market_history_batch",
    autoretry_for=_TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=3,
    time_limit=300,
    soft_time_limit=270,
)
@record_task_run("market.tasks.fetch_market_history_batch")
def fetch_market_history_batch(exchange: str, codes: list[str]):
    """Fetch a bounded symbol batch so one slow/bad symbol cannot exhaust a
    whole-universe task.  Individual fetchers return failed-symbol samples
    rather than aborting the remaining codes.

    DSE only: the network phase (settings.MARKET_FETCH_MODE-controlled —
    sequential by default, see market.services.concurrent_fetch) runs
    outside the DB write lock, exactly like
    market.management.commands.fetch_history's existing network/persist
    split; only the persist phase (sync_dse_history's unchanged
    save_history loop) runs under exclusive_db_write."""
    from datetime import timedelta

    from django.conf import settings
    from django.utils import timezone

    from market.models import Exchange
    from market.services.autosync import exclusive_db_write
    from market.services.concurrent_fetch import prefetch_dse_history
    from market.services.dse_fetcher import sync_dse_history
    from market.services.cse_fetcher import sync_cse_history

    if exchange == Exchange.DSE:
        end = timezone.localdate()
        start = end - timedelta(days=settings.LOOKBACK_DAYS)
        prefetched, fetch_stats = prefetch_dse_history(codes, start, end)
        with exclusive_db_write(blocking=True, timeout=300):
            result = sync_dse_history(codes=codes, use_synthetic_fallback=False, _prefetched=prefetched, _fetch_stats=fetch_stats)
    elif exchange == Exchange.CSE:
        with exclusive_db_write(blocking=True, timeout=300):
            result = sync_cse_history(codes=codes, use_synthetic_fallback=False)
    else:
        raise ValueError(f"Unknown exchange: {exchange}")
    failed = int(result.get("failed_or_fallback", 0) or 0) + int(result.get("skipped", 0) or 0)
    return result | {
        "partial": bool(failed), "exchange": exchange,
        "symbols_attempted": len(codes), "symbols_successful": int(result.get("ok", 0) or 0),
        "symbols_failed": failed,
    }


@shared_task(name="market.tasks.reconcile_orphaned_task_runs", time_limit=60, soft_time_limit=45)
@record_task_run("market.tasks.reconcile_orphaned_task_runs")
def reconcile_orphaned_task_runs_task():
    """Mark TaskRun rows abandoned by a hard-killed worker as failures."""
    from market.services.task_status import reconcile_orphaned_task_runs

    return reconcile_orphaned_task_runs()


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
    from market.services.locking import LockBusy, distributed_lock

    try:
        # Own non-blocking lock, same reasoning as train_ml_model's —
        # only one full-analysis run at a time; a duplicate (manual +
        # scheduled colliding) is Skipped, not queued-then-Failed.
        with distributed_lock("full-analysis", timeout=900, blocking_timeout=0):
            with exclusive_db_write(blocking=True, timeout=600):
                return run_full_analysis(train_ml=train_ml)
    except LockBusy:
        return {"ok": True, "skipped": "already_running"}


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
    name="market.tasks.run_paper_trading",
    autoretry_for=_TRANSIENT_ERRORS,
    retry_backoff=True,
    max_retries=2,
    time_limit=120,
    soft_time_limit=100,
)
@record_task_run("market.tasks.run_paper_trading")
def run_paper_trading(force: bool = False):
    """Execute one virtual-only cycle from stored predictions and prices."""
    from django.conf import settings
    from market.services.autosync import exclusive_db_write
    from django.utils import timezone
    from market.services.paper_trading import ensure_account, run_autonomous_cycle, trading_window_status

    if not getattr(settings, "AUTO_PAPER_TRADING", True) and not force:
        return {"ok": True, "skipped": "disabled"}
    if not force:
        window = trading_window_status()
        if not window["is_open"]:
            return {"ok": True, "skipped": "outside_paper_trading_window", "window": window}
        account = ensure_account()
        interval = int(getattr(settings, "AUTO_PAPER_TRADING_INTERVAL", 900))
        if account.last_run_at and (timezone.now() - account.last_run_at).total_seconds() < interval:
            return {"ok": True, "skipped": "interval_not_elapsed", "interval_seconds": interval}
    with exclusive_db_write(blocking=True, timeout=90):
        return run_autonomous_cycle(force=force)


@shared_task(name="market.tasks.finalize_paper_trading_day", time_limit=60, soft_time_limit=45)
@record_task_run("market.tasks.finalize_paper_trading_day")
def finalize_paper_trading_day():
    """Record the 14:25 virtual closing statement without placing trades."""
    from django.conf import settings
    from market.services.autosync import exclusive_db_write
    from market.services.paper_trading import ensure_account, record_equity_snapshot

    if not getattr(settings, "AUTO_PAPER_TRADING", True):
        return {"ok": True, "skipped": "disabled"}
    with exclusive_db_write(blocking=True, timeout=45):
        summary = record_equity_snapshot(ensure_account())
    return {"ok": True, "equity": str(summary["total_equity"]), "profit_loss": str(summary["total_return"])}


@shared_task(
    name="market.tasks.sync_pe_ratios",
    autoretry_for=_TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=3,
    time_limit=90,
    soft_time_limit=75,
)
@record_task_run("market.tasks.sync_pe_ratios")
def sync_pe_ratios():
    """Daily DSE trailing-P/E snapshot refresh (display only — see
    sync_dse_pe_ratios's docstring on why this isn't an ML feature)."""
    from market.services.autosync import exclusive_db_write
    from market.services.dse_fetcher import sync_dse_pe_ratios

    with exclusive_db_write(blocking=True, timeout=60):
        return sync_dse_pe_ratios()


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
    analysis pass (which can also optionally retrain inline). Scheduled
    daily, outside market hours (see beat_schedule's train-ml-model-daily,
    AUTO_ML_TRAINING_TIME) — this self-check is defense in depth so a
    manual trigger or a direct call can't accidentally retrain because
    AUTO_ML_TRAINING is off, and market.services.ml_model.train_model()
    itself already restricts training to enabled_exchanges() only (CSE
    excluded when disabled) and no-ops when there's no new
    label-resolvable data since the last successful train."""
    from django.conf import settings

    from market.services.autosync import exclusive_db_write
    from market.services.ml_model import train_model

    if not getattr(settings, "AUTO_ML_TRAINING", True):
        return {"ok": True, "skipped": "disabled"}

    from market.services.locking import LockBusy, distributed_lock

    try:
        # Own non-blocking lock (checked before the blocking DB-write
        # lock) so a duplicate trigger — a manual admin click while the
        # weekly scheduled run is already training — is reported as
        # Skipped immediately rather than queueing behind it and
        # eventually timing out as a Failure.
        with distributed_lock("ml-training", timeout=600, blocking_timeout=0):
            with exclusive_db_write(blocking=True, timeout=300):
                return train_model()
    except LockBusy:
        return {"ok": True, "skipped": "already_running"}


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
    from django.conf import settings
    from django.utils import timezone

    from market.services.autosync import exclusive_db_write
    from market.services.close_learn import run_close_learn_cycle

    if not getattr(settings, "AUTO_CLOSE_LEARN", True):
        return {"ok": True, "skipped": "disabled"}

    with exclusive_db_write(blocking=True, timeout=300):
        return run_close_learn_cycle(as_of=timezone.localdate(), train=True)


@shared_task(name="market.tasks.run_shadow_model", time_limit=600, soft_time_limit=540)
@record_task_run("market.tasks.run_shadow_model")
def run_shadow_model():
    from market.services.shadow_model import run_shadow_cycle
    return run_shadow_cycle()


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
    name="market.tasks.assess_ml_reliability",
    autoretry_for=_TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=120,
    max_retries=2,
    time_limit=600,
    soft_time_limit=540,
)
@record_task_run("market.tasks.assess_ml_reliability")
def assess_ml_reliability():
    """Capture today's predictions, settle due outcomes, and assess both
    model families against rolling windows. Never activates/deactivates a
    model itself — see market.services.reliability_recommend."""
    from market.services.autosync import exclusive_db_write
    from market.services.reliability_report import run_reliability_assessment

    with exclusive_db_write(blocking=True, timeout=300):
        return run_reliability_assessment()


@shared_task(
    name="market.tasks.run_end_of_day_pipeline",
    time_limit=1800,
    soft_time_limit=1700,
)
@record_task_run("market.tasks.run_end_of_day_pipeline")
def run_end_of_day_pipeline():
    """On-demand (manual Admin button) or automated end-of-day sequence:

        append final daily bars (+ full analysis, if AUTO_ANALYZE_AFTER_APPEND)
        -> settle previous forecasts + generate next-session forecasts (close-learn)
        -> reliability/drift assessment
        -> send eligible alerts (daily digest)

    Each stage is the *exact same* task function the automated beat
    schedule calls (append_daily_bars/close_learn_settlement/
    assess_ml_reliability/send_daily_digest) — calling them directly
    (not via .delay()) still runs them through their own @record_task_run
    decoration, so each stage gets its own independent TaskRun row in
    addition to this orchestrator's. A stage that raises is caught and
    recorded in *this* task's detail as a failed stage — it does not
    silently mark the whole pipeline "ok" — but later stages still run:
    each one's own prerequisites are read from DB state (has an
    unresolved forecast/has fresh price data), not from the immediately
    preceding stage's return value, so e.g. a close-learn failure
    shouldn't by itself prevent a reliability assessment of whatever
    already-settled data exists.
    """
    from notifications.tasks import send_daily_digest

    stages: dict[str, dict] = {}
    ok = True

    for stage_name, fn in (
        ("append", append_daily_bars),
        ("close_learn_settlement", close_learn_settlement),
        ("reliability_assessment", assess_ml_reliability),
        ("digest", send_daily_digest),
    ):
        try:
            result = fn()
        except Exception as exc:
            result = {"ok": False, "error": str(exc)[:500]}
            ok = False
        else:
            if isinstance(result, dict) and result.get("ok") is False:
                ok = False
        stages[stage_name] = result

    return {"ok": ok, "stages": stages}


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
