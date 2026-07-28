"""
Daily market data append schedule (Asia/Dhaka).

Runs at 10:05 and 14:05 on trading days (Sun–Thu).
Appends/updates today's OHLC bars without wiping past history.
Manual "Fetch live + analyze" on the dashboard remains available anytime.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import time as dtime

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from market.services.autosync import exclusive_db_write

logger = logging.getLogger(__name__)

_APPEND_TIMES = (dtime(10, 5), dtime(14, 5))
_thread_started = False
_last_run_key: str | None = None
_last_close_learn_key: str | None = None
_CLOSE_LEARN_TIME = dtime(14, 45)


def append_today_bars_unlocked() -> dict:
    from market.services.autosync import _run_live_sync_unlocked

    return _run_live_sync_unlocked()


def run_scheduled_append() -> dict:
    """Append today's live bars, then refresh analysis."""
    close_old_connections()
    try:
        with exclusive_db_write(blocking=True, timeout=300):
            live = append_today_bars_unlocked()
            analyze_info = None
            if live.get("ok") and getattr(settings, "AUTO_ANALYZE_AFTER_APPEND", True):
                from market.services.analyzer import run_full_analysis

                now = timezone.localtime()
                train = now.hour >= 14
                analyze_info = run_full_analysis(train_ml=train)
            return {"live": live, "analysis": analyze_info}
    finally:
        close_old_connections()


def _should_run_now(now=None) -> bool:
    global _last_run_key
    now = now or timezone.localtime()
    if now.weekday() not in (6, 0, 1, 2, 3):  # Sun–Thu
        return False
    current = dtime(now.hour, now.minute)
    if current not in _APPEND_TIMES:
        return False
    key = f"{now.date().isoformat()}|{current.strftime('%H:%M')}"
    return key != _last_run_key


def _mark_ran(now=None):
    global _last_run_key
    now = now or timezone.localtime()
    current = dtime(now.hour, now.minute)
    _last_run_key = f"{now.date().isoformat()}|{current.strftime('%H:%M')}"


def _should_run_close_learn(now=None) -> bool:
    """After close + 14:05 append: run next-day close forecasts + settle/learn."""
    global _last_close_learn_key
    now = now or timezone.localtime()
    if not getattr(settings, "AUTO_CLOSE_LEARN", True):
        return False
    if now.weekday() not in (6, 0, 1, 2, 3):
        return False
    current = dtime(now.hour, now.minute)
    if current != _CLOSE_LEARN_TIME:
        return False
    key = f"{now.date().isoformat()}|close-learn"
    return key != _last_close_learn_key


def _mark_close_learn_ran(now=None):
    global _last_close_learn_key
    now = now or timezone.localtime()
    _last_close_learn_key = f"{now.date().isoformat()}|close-learn"


def run_close_learn_job() -> dict:
    close_old_connections()
    try:
        with exclusive_db_write(blocking=True, timeout=300):
            from market.services.close_learn import run_close_learn_cycle

            return run_close_learn_cycle(as_of=timezone.localdate(), train=True)
    finally:
        close_old_connections()


def _loop():
    time.sleep(12)
    logger.info("Daily append scheduler active (10:05 & 14:05 Asia/Dhaka)")
    logger.info("Close-learn scheduler active (14:45 Asia/Dhaka, next-day close)")
    while True:
        try:
            if getattr(settings, "AUTO_DAILY_APPEND", True) and _should_run_now():
                logger.info("Running scheduled daily append…")
                _mark_ran()
                result = run_scheduled_append()
                logger.info("Scheduled append finished: %s", result)
            if _should_run_close_learn():
                logger.info("Running after-close next-day learn cycle…")
                _mark_close_learn_ran()
                learn_result = run_close_learn_job()
                logger.info("Close-learn finished: %s", learn_result)
        except Exception as exc:
            logger.exception("Scheduled append/learn failed: %s", exc)
        finally:
            close_old_connections()
        time.sleep(20)


def start_daily_append_thread():
    global _thread_started
    if _thread_started:
        return
    if not getattr(settings, "AUTO_DAILY_APPEND", True):
        return
    import os
    import sys

    is_runserver = any("runserver" in str(a) for a in sys.argv)
    if is_runserver and os.environ.get("RUN_MAIN") != "true":
        return

    t = threading.Thread(target=_loop, name="bazaar-daily-append", daemon=True)
    t.start()
    _thread_started = True
