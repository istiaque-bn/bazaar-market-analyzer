"""
Daily market data append (Asia/Dhaka).

Invoked by the market.tasks.append_daily_bars Celery task, scheduled at
10:05 and 14:05 Sun-Thu (see config/celery.py's beat_schedule) — not a
background thread. Appends/updates today's OHLC bars without wiping past
history. Manual "Fetch live + analyze" on the dashboard remains available
anytime (staff-only, also enqueues a task).
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from market.services.autosync import exclusive_db_write

logger = logging.getLogger(__name__)


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
