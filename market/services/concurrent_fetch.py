"""Bounded concurrent fetch of DSE per-symbol history.

market.services.dse_fetcher.fetch_dse_history tries the bdshare library
first (an opaque, synchronous third-party client we cannot verify is
event-loop-safe) and falls back to scraping dsebd.org's day-end archive.
Because the dominant per-symbol cost is that opaque blocking call, there is
no client-level API here that supports *native* asyncio without either (a)
dropping bdshare and losing ~1.5 years of history coverage, or (b)
duplicating dsebd.org's fragile HTML-table-parsing logic to reimplement the
archive leg against an async HTTP client. Both were judged too risky for
this phase (see the Aug 2026 concurrency plan). Instead:

- "threadpool" mode runs the existing, unmodified `fetch_dse_history` per
  symbol in a bounded `ThreadPoolExecutor` — this is the safe, direct fix
  for the confirmed bottleneck (sequential per-symbol requests).
- "asyncio" mode runs the *same* unmodified `fetch_dse_history` per symbol
  via `loop.run_in_executor`, bounded by an `asyncio.Semaphore` — included
  so the benchmark empirically confirms (rather than assumes) whether
  asyncio's dispatch adds any benefit over raw threads for this specific
  executor-bound workload. Expect it to perform the same as or slightly
  worse than "threadpool", never better, since the underlying work is
  identical.
- "sequential" mode is today's existing behavior (the default — see
  settings.MARKET_FETCH_MODE), reproduced here so all three modes share one
  call site and one stats/result shape.

Every symbol is independently fault-isolated: one failure, timeout, or
retry exhaustion never cancels or blocks any other symbol's fetch.
"""
from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from datetime import date

import pandas as pd
import tenacity
from django.conf import settings

from market.services.dse_fetcher import fetch_dse_history

logger = logging.getLogger(__name__)

# HTTP statuses worth retrying (transient) vs. giving up immediately on.
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


@dataclass
class FetchOutcome:
    code: str
    df: pd.DataFrame | None
    source: str
    status: str  # "success" | "failure" | "timeout"
    duration_seconds: float
    retry_count: int
    error: str | None = None
    rate_limited: bool = False


@dataclass
class _RunStats:
    rate_limit_hits: int = 0
    statuses_seen: list = field(default_factory=list)


def _attempt_once(code: str, start: date, end: date, timeout: int, run_stats: _RunStats) -> tuple[pd.DataFrame | None, str]:
    """One fetch_dse_history call, recording any HTTP status the archive
    leg observed (bdshare's internal requests are opaque and not visible
    here — see module docstring)."""

    def _on_status(status_code: int) -> None:
        run_stats.statuses_seen.append(status_code)
        if status_code == 429 or status_code in _RETRYABLE_STATUSES:
            run_stats.rate_limit_hits += 1 if status_code == 429 else 0

    return fetch_dse_history(code, start, end, timeout=timeout, status_callback=_on_status)


def _fetch_one_sync(code: str, start: date, end: date, *, timeout: int, max_retries: int) -> FetchOutcome:
    """Fetch one symbol with bounded retry/backoff. Never raises — every
    failure mode (exception, timeout, exhausted retries, empty result)
    is captured into the returned FetchOutcome so one bad symbol can never
    take down a batch."""
    started = time.monotonic()
    run_stats = _RunStats()
    retryer = tenacity.Retrying(
        stop=tenacity.stop_after_attempt(max_retries + 1),
        wait=tenacity.wait_exponential_jitter(initial=1, max=8),
        # fetch_dse_history never raises on a transient failure — its
        # helpers already catch their own exceptions and return None (see
        # dse_fetcher.py) — so "retry-worthy" means "got nothing back",
        # not "raised an exception".
        retry=tenacity.retry_if_result(lambda r: r[0] is None or r[0].empty),
        reraise=True,
    )
    try:
        df, source = retryer(_attempt_once, code, start, end, timeout, run_stats)
        retry_count = retryer.statistics.get("attempt_number", 1) - 1
        rate_limited = run_stats.rate_limit_hits > 0
        if df is None or df.empty:
            return FetchOutcome(
                code=code, df=None, source=source, status="failure",
                duration_seconds=time.monotonic() - started, retry_count=retry_count,
                error="no data returned", rate_limited=rate_limited,
            )
        return FetchOutcome(
            code=code, df=df, source=source, status="success",
            duration_seconds=time.monotonic() - started, retry_count=retry_count,
            rate_limited=rate_limited,
        )
    except Exception as exc:  # last retry attempt itself raised
        retry_count = retryer.statistics.get("attempt_number", 1) - 1
        status = "timeout" if isinstance(exc, TimeoutError) else "failure"
        logger.warning("Concurrent DSE fetch failed for %s after %s attempts: %s", code, retry_count + 1, exc)
        return FetchOutcome(
            code=code, df=None, source="none", status=status,
            duration_seconds=time.monotonic() - started, retry_count=retry_count,
            error=f"{type(exc).__name__}: {exc}", rate_limited=run_stats.rate_limit_hits > 0,
        )


def _fetch_threaded(codes: list[str], start: date, end: date, *, concurrency: int, timeout: int, max_retries: int) -> list[FetchOutcome]:
    outcomes: dict[str, FetchOutcome] = {}
    active_concurrency = concurrency
    consecutive_rate_limits = 0
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="dse-fetch") as pool:
        futures = {pool.submit(_fetch_one_sync, code, start, end, timeout=timeout, max_retries=max_retries): code for code in codes}
        for future in futures:
            code = futures[future]
            try:
                outcome = future.result(timeout=timeout * (max_retries + 2))
            except FutureTimeoutError:
                # Python cannot forcibly kill a running thread; the
                # straggler is abandoned here (it will finish naturally in
                # the background and its result discarded) rather than
                # blocking the whole batch on it. The bounded pool size
                # caps how many stragglers can accumulate at once.
                outcome = FetchOutcome(
                    code=code, df=None, source="none", status="timeout",
                    duration_seconds=float(timeout * (max_retries + 2)), retry_count=max_retries,
                    error="future.result() timed out — worker thread abandoned",
                )
            outcomes[code] = outcome
            if outcome.rate_limited:
                consecutive_rate_limits += 1
                if consecutive_rate_limits >= 3 and active_concurrency > 1:
                    active_concurrency = max(1, active_concurrency // 2)
                    logger.warning(
                        "DSE fetch: %s rate-limit signals seen — recommend reducing MARKET_FETCH_CONCURRENCY "
                        "to %s for subsequent batches (this run's pool size is already fixed).",
                        consecutive_rate_limits, active_concurrency,
                    )
            else:
                consecutive_rate_limits = 0
    return [outcomes[c] for c in codes]


async def _fetch_async(codes: list[str], start: date, end: date, *, concurrency: int, timeout: int, max_retries: int) -> list[FetchOutcome]:
    semaphore = asyncio.Semaphore(concurrency)
    loop = asyncio.get_running_loop()
    # A dedicated executor (not the default loop-wide one) keeps this mode's
    # thread footprint identical to and directly comparable against
    # "threadpool" mode's ThreadPoolExecutor(max_workers=concurrency).
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="dse-fetch-async") as pool:

        async def _bounded(code: str) -> FetchOutcome:
            async with semaphore:
                try:
                    return await asyncio.wait_for(
                        loop.run_in_executor(pool, lambda: _fetch_one_sync(code, start, end, timeout=timeout, max_retries=max_retries)),
                        timeout=timeout * (max_retries + 2),
                    )
                except asyncio.TimeoutError:
                    return FetchOutcome(
                        code=code, df=None, source="none", status="timeout",
                        duration_seconds=float(timeout * (max_retries + 2)), retry_count=max_retries,
                        error="asyncio.wait_for timed out — worker thread abandoned",
                    )

        return list(await asyncio.gather(*(_bounded(code) for code in codes)))


def _fetch_sequential(codes: list[str], start: date, end: date, *, timeout: int, max_retries: int) -> list[FetchOutcome]:
    return [_fetch_one_sync(code, start, end, timeout=timeout, max_retries=max_retries) for code in codes]


def prefetch_dse_history(
    codes: list[str],
    start: date,
    end: date,
    *,
    mode: str | None = None,
    concurrency: int | None = None,
    timeout: int | None = None,
    max_retries: int | None = None,
) -> tuple[list[tuple[str, pd.DataFrame | None, str]], dict]:
    """Network-phase fetch for a batch of DSE codes — no DB writes, no lock
    held (callers persist via the existing sync_dse_history(...,
    _prefetched=...) seam under exclusive_db_write, exactly as
    market/management/commands/fetch_history.py already does).

    Returns (prefetched, stats):
      prefetched — list[(code, df|None, source)], the exact shape
        sync_dse_history's `_prefetched` parameter expects.
      stats — dict of attempted/successful/failed/timed_out/retried counts
        plus mode/concurrency/duration_seconds, for sync_dse_history's
        `_fetch_stats` parameter and for observability/logging.
    """
    mode = mode or settings.MARKET_FETCH_MODE
    concurrency = concurrency or settings.MARKET_FETCH_CONCURRENCY
    timeout = timeout or settings.MARKET_FETCH_TIMEOUT
    max_retries = max_retries if max_retries is not None else settings.MARKET_FETCH_MAX_RETRIES

    started = time.monotonic()
    if not codes:
        outcomes: list[FetchOutcome] = []
    elif mode == "threadpool":
        outcomes = _fetch_threaded(codes, start, end, concurrency=concurrency, timeout=timeout, max_retries=max_retries)
    elif mode == "asyncio":
        outcomes = asyncio.run(_fetch_async(codes, start, end, concurrency=concurrency, timeout=timeout, max_retries=max_retries))
    else:
        outcomes = _fetch_sequential(codes, start, end, timeout=timeout, max_retries=max_retries)
    duration = time.monotonic() - started

    prefetched = [(o.code, o.df, o.source) for o in outcomes]
    stats = {
        "mode": mode,
        "concurrency": concurrency if mode != "sequential" else 1,
        "attempted": len(outcomes),
        "successful": sum(1 for o in outcomes if o.status == "success"),
        "failed": sum(1 for o in outcomes if o.status == "failure"),
        "timed_out": sum(1 for o in outcomes if o.status == "timeout"),
        "retried": sum(o.retry_count for o in outcomes),
        "rate_limited_symbols": sum(1 for o in outcomes if o.rate_limited),
        "duration_seconds": round(duration, 3),
    }
    logger.info(
        "DSE prefetch (%s, concurrency=%s): %s/%s ok in %.1fs (failed=%s timed_out=%s retried=%s)",
        stats["mode"], stats["concurrency"], stats["successful"], stats["attempted"],
        duration, stats["failed"], stats["timed_out"], stats["retried"],
    )
    return prefetched, stats
