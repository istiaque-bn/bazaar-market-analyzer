"""
Automatic live market sync for the ticker.

Runs a background loop while Django is up (no Redis required) and also
refreshes on-demand when /ticker.json is polled and data is stale.

Uses a process-wide write lock so SQLite is not hit by pipeline + autosync
at the same time ("database is locked").
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections, connection
from django.db.utils import OperationalError
from django.utils import timezone

logger = logging.getLogger(__name__)

# Shared lock for any heavy SQLite writer (autosync, pipeline, seed)
db_write_lock = threading.Lock()

_state = {
    "last_attempt": None,
    "last_success": None,
    "last_error": None,
    "last_result": None,
    "running": False,
    "source": None,
}
_thread_started = False


def _state_file() -> Path:
    path = Path(settings.CACHE_DIR) / "autosync_state.txt"
    return path


def get_sync_status() -> dict:
    return {
        "enabled": getattr(settings, "AUTO_MARKET_SYNC", True),
        "last_attempt": _state["last_attempt"].isoformat() if _state["last_attempt"] else None,
        "last_success": _state["last_success"].isoformat() if _state["last_success"] else None,
        "last_error": _state["last_error"],
        "running": _state["running"],
        "source": _state["source"],
        "result": _state["last_result"],
        "market_open": is_market_hours(),
    }


def is_market_hours(now=None) -> bool:
    """DSE/CSE regular session: Sun–Thu, ~10:00–14:45 Asia/Dhaka."""
    now = now or timezone.localtime()
    if now.weekday() not in (6, 0, 1, 2, 3):
        return False
    t = now.time()
    start = datetime.strptime("09:55", "%H:%M").time()
    end = datetime.strptime("15:00", "%H:%M").time()
    return start <= t <= end


def sync_interval_seconds() -> int:
    if is_market_hours():
        return int(getattr(settings, "AUTO_SYNC_INTERVAL_MARKET", 60))
    return int(getattr(settings, "AUTO_SYNC_INTERVAL_OFF", 900))


@contextmanager
def exclusive_db_write(blocking: bool = True, timeout: float = 120.0):
    """Serialize SQLite writers across threads."""
    if blocking:
        acquired = db_write_lock.acquire(timeout=timeout)
    else:
        acquired = db_write_lock.acquire(blocking=False)
    if not acquired:
        raise TimeoutError("Could not acquire database write lock")
    close_old_connections()
    try:
        yield
    finally:
        close_old_connections()
        db_write_lock.release()


def configure_sqlite():
    """Enable WAL + busy timeout so concurrent readers/writers wait instead of failing."""
    try:
        if connection.vendor != "sqlite":
            return
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA busy_timeout=60000;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
    except Exception as exc:
        logger.warning("SQLite pragma setup failed: %s", exc)


def _update_snapshots_from_quotes(exchange: str, count: int, source: str):
    from market.models import MarketSnapshot, Stock

    stocks = Stock.objects.filter(exchange=exchange, is_active=True, last_change_pct__isnull=False)
    adv = stocks.filter(last_change_pct__gt=0).count()
    dec = stocks.filter(last_change_pct__lt=0).count()
    unc = stocks.filter(last_change_pct=0).count()
    MarketSnapshot.objects.update_or_create(
        exchange=exchange,
        as_of=timezone.localdate(),
        defaults={
            "advancers": adv,
            "decliners": dec,
            "unchanged": unc,
            "notes": f"Auto-sync via {source} ({count} quotes)",
        },
    )


def _retry_db(fn, attempts: int = 5, delay: float = 0.4):
    last_exc = None
    for i in range(attempts):
        try:
            return fn()
        except OperationalError as exc:
            last_exc = exc
            if "locked" not in str(exc).lower():
                raise
            time.sleep(delay * (i + 1))
            close_old_connections()
    raise last_exc  # type: ignore[misc]


def run_live_sync(force: bool = False) -> dict:
    """Fetch live DSE/CSE quotes into the DB. Safe to call often (locked)."""
    if not getattr(settings, "AUTO_MARKET_SYNC", True) and not force:
        return {"ok": False, "skipped": "disabled"}

    try:
        with exclusive_db_write(blocking=False):
            return _run_live_sync_unlocked()
    except TimeoutError:
        return {"ok": False, "skipped": "already_running", **get_sync_status()}


def _run_live_sync_unlocked() -> dict:
    _state["running"] = True
    _state["last_attempt"] = timezone.now()
    try:
        from market.services.cse_fetcher import sync_cse_live
        from market.services.dse_fetcher import sync_dse_live

        dse = _retry_db(sync_dse_live)
        cse = _retry_db(sync_cse_live)
        source = None
        if dse.get("ok"):
            source = dse.get("source")
            _retry_db(lambda: _update_snapshots_from_quotes("DSE", dse.get("count") or 0, source or "dse"))
        if cse.get("ok"):
            _retry_db(lambda: _update_snapshots_from_quotes("CSE", cse.get("count") or 0, cse.get("source") or "cse"))
            source = source or cse.get("source")

        ok = bool(dse.get("ok") or cse.get("ok"))
        result = {"ok": ok, "dse": dse, "cse": cse, "at": timezone.now().isoformat()}
        _state["last_result"] = result
        _state["source"] = source
        if ok:
            _state["last_success"] = timezone.now()
            _state["last_error"] = None
            try:
                _state_file().write_text(timezone.now().isoformat(), encoding="utf-8")
            except OSError:
                pass
        else:
            _state["last_error"] = str(dse.get("error") or cse.get("error") or "fetch failed")
        return result
    except Exception as exc:
        logger.exception("Auto market sync failed: %s", exc)
        _state["last_error"] = str(exc)
        return {"ok": False, "error": str(exc)}
    finally:
        _state["running"] = False


def maybe_sync(force: bool = False) -> dict:
    """Sync if stale based on market/off-hours interval."""
    interval = sync_interval_seconds()
    last = _state["last_success"] or _state["last_attempt"]
    if not force and last is not None:
        age = (timezone.now() - last).total_seconds()
        if age < interval:
            return {"ok": True, "skipped": "fresh", "age_seconds": age, **get_sync_status()}
    return run_live_sync(force=force)


def _loop():
    time.sleep(8)
    while True:
        try:
            if getattr(settings, "AUTO_MARKET_SYNC", True):
                maybe_sync(force=False)
        except Exception as exc:
            logger.warning("Autosync loop error: %s", exc)
        finally:
            close_old_connections()
        time.sleep(max(15, sync_interval_seconds() // 2))


def start_autosync_thread():
    global _thread_started
    if _thread_started:
        return
    if not getattr(settings, "AUTO_MARKET_SYNC", True):
        return
    import os
    import sys

    is_runserver = any("runserver" in str(a) for a in sys.argv)
    if is_runserver and os.environ.get("RUN_MAIN") != "true":
        return

    configure_sqlite()
    t = threading.Thread(target=_loop, name="bazaar-market-autosync", daemon=True)
    t.start()
    _thread_started = True
    logger.info(
        "Bazaar autosync started (market=%ss off=%ss)",
        getattr(settings, "AUTO_SYNC_INTERVAL_MARKET", 60),
        getattr(settings, "AUTO_SYNC_INTERVAL_OFF", 900),
    )
