"""
Phase 6 — market-data provenance and quality controls.

Two complementary mechanisms:

  1. Provenance labeling (forward-looking): every write path tags new/
     updated PriceHistory rows with an honest `source`, `is_synthetic`,
     `fetched_at` and `import_batch`. Legacy rows default to
     source=unknown, is_synthetic=False, fetched_at=None — never guessed
     as live (see PriceHistoryQuerySet.live() in market/models.py).

  2. Quality flagging (works on any row, new or legacy, because it's
     derived from the data values themselves, not from unknowable
     write-time metadata): impossible OHLC, abnormal single-day jumps,
     stale-quote runs (a "duplicate" signal — see module docstring in
     market/tests/test_data_quality.py), and missing-session gaps.
     Flags are stored, never used to delete a row — "never silently
     discard evidence".
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import date

from django.db import transaction
from django.utils import timezone

from market.models import (
    DataSource,
    Exchange,
    ImportBatch,
    PriceHistory,
    PriceHistoryRevision,
    Stock,
)

logger = logging.getLogger(__name__)

ABNORMAL_JUMP_PCT = 25.0
GAP_CALENDAR_DAYS = 10  # beyond a normal Sun-Thu week + one holiday week
STALE_RUN_MIN_LENGTH = 5
REVISION_EPSILON = 1e-9


def validate_ohlcv(open_, high, low, close, volume) -> list[str]:
    """Pure function: returns violation codes for a single bar. Never
    raises, never mutates — callers decide what to do with the flags
    (store them; never used to reject a write, per requirement 4)."""
    flags: list[str] = []
    for name, val in (("open", open_), ("high", high), ("low", low), ("close", close)):
        if val is None or val <= 0:
            flags.append(f"non_positive_{name}")
    if volume is not None and volume < 0:
        flags.append("negative_volume")
    if high is not None and low is not None and high >= 0 and low >= 0:
        if high < low:
            flags.append("high_below_low")
        else:
            if open_ is not None and open_ > 0 and not (low <= open_ <= high):
                flags.append("open_out_of_range")
            if close is not None and close > 0 and not (low <= close <= high):
                flags.append("close_out_of_range")
    return flags


def create_import_batch(source: str, exchange: str = "", **request_meta) -> ImportBatch:
    return ImportBatch.objects.create(source=source, exchange=exchange or "", request_meta=request_meta)


def finish_import_batch(batch: ImportBatch | None, *, row_count: int = 0, stock_count: int = 0, error: str = "", notes: str = "") -> None:
    if batch is None:
        return
    batch.finished_at = timezone.now()
    batch.row_count = row_count
    batch.stock_count = stock_count
    if error:
        batch.error = error
    if notes:
        batch.notes = notes
    batch.save(update_fields=["finished_at", "row_count", "stock_count", "error", "notes"])


def _materially_different(existing: PriceHistory, new_values: dict) -> bool:
    for field in ("open", "high", "low", "close"):
        if field in new_values and abs((getattr(existing, field) or 0.0) - float(new_values[field])) > REVISION_EPSILON:
            return True
    if "volume" in new_values and int(existing.volume or 0) != int(new_values["volume"] or 0):
        return True
    return False


def snapshot_revision_if_changed(existing: PriceHistory, new_values: dict, *, new_source: str, reason: str) -> bool:
    """Before an existing row's price/volume values are overwritten,
    record what they were. Returns True if a revision was written."""
    if not _materially_different(existing, new_values):
        return False
    PriceHistoryRevision.objects.create(
        price_history=existing,
        stock_id=existing.stock_id,
        date=existing.date,
        previous_open=existing.open,
        previous_high=existing.high,
        previous_low=existing.low,
        previous_close=existing.close,
        previous_volume=existing.volume,
        previous_source=existing.source,
        new_source=new_source,
        reason=reason,
    )
    return True


def blocks_synthetic_overwrite(existing: PriceHistory | None, incoming_source: str) -> bool:
    """True if writing `incoming_source` data would overwrite a row that
    already has a *confirmed real* (non-unknown, non-synthetic) source —
    protects genuine data from being clobbered by demo/fallback writes."""
    from market.models import SYNTHETIC_SOURCES

    if existing is None or incoming_source not in SYNTHETIC_SOURCES:
        return False
    return existing.source != DataSource.UNKNOWN and existing.source not in SYNTHETIC_SOURCES


def scan_stock_quality(stock: Stock) -> tuple[dict[int, list[str]], Counter, list[dict]]:
    """Returns (flags_by_row_id, flag_counts, gaps) for one stock's full
    history, ordered chronologically."""
    rows = list(stock.prices.order_by("date"))
    flags_by_id: dict[int, set] = {}
    counts: Counter = Counter()
    gaps: list[dict] = []
    prev = None
    stale_run: list[PriceHistory] = []

    def _flush_stale_run():
        if len(stale_run) >= STALE_RUN_MIN_LENGTH:
            for sr in stale_run:
                flags_by_id.setdefault(sr.id, set()).add("stale_quote_run")

    for row in rows:
        row_flags = validate_ohlcv(row.open, row.high, row.low, row.close, row.volume)
        if prev is not None:
            gap_days = (row.date - prev.date).days
            if gap_days > GAP_CALENDAR_DAYS:
                row_flags.append("gap_before")
                gaps.append(
                    {
                        "stock_id": stock.id,
                        "trading_code": stock.trading_code,
                        "exchange": stock.exchange,
                        "gap_start": prev.date.isoformat(),
                        "gap_end": row.date.isoformat(),
                        "days_missing": gap_days,
                    }
                )
            if prev.close and prev.close > 0 and row.close and row.close > 0:
                jump = abs(row.close - prev.close) / prev.close * 100
                if jump >= ABNORMAL_JUMP_PCT:
                    row_flags.append("abnormal_jump")
            if row.close == prev.close and row.volume and row.volume > 0:
                stale_run.append(row)
            else:
                _flush_stale_run()
                stale_run = []
        for f in row_flags:
            flags_by_id.setdefault(row.id, set()).add(f)
            counts[f] += 1
        prev = row
    _flush_stale_run()
    counts["stale_quote_run"] = sum(1 for f in flags_by_id.values() if "stale_quote_run" in f)

    return {k: sorted(v) for k, v in flags_by_id.items()}, counts, gaps


def run_quality_scan(exchange: str | None = None, limit_stocks: int | None = None, max_gaps_reported: int = 200) -> dict:
    """Scans PriceHistory rows (all provenance — this works on data
    values, not write-time metadata) and rewrites `quality_flags` to
    reflect exactly what's currently detected. Never deletes a row."""
    qs = Stock.objects.all()
    if exchange:
        qs = qs.filter(exchange=exchange)
    if limit_stocks:
        qs = qs[:limit_stocks]

    total_counts: Counter = Counter()
    all_gaps: list[dict] = []
    rows_flagged = 0
    rows_scanned = 0
    stocks_scanned = 0

    for stock in qs.iterator():
        flags_by_id, counts, gaps = scan_stock_quality(stock)
        stocks_scanned += 1
        rows_scanned += stock.prices.count()
        total_counts.update(counts)
        all_gaps.extend(gaps)

        # Clear stale flags on rows that no longer have any, and set fresh
        # flags on the ones that do — always an explicit list, never lost.
        to_update = []
        for row in stock.prices.only("id", "quality_flags"):
            new_flags = flags_by_id.get(row.id, [])
            if row.quality_flags != new_flags:
                row.quality_flags = new_flags
                to_update.append(row)
        if to_update:
            PriceHistory.objects.bulk_update(to_update, ["quality_flags"], batch_size=500)
        rows_flagged += len(flags_by_id)

    return {
        "ok": True,
        "scanned_at": timezone.now().isoformat(),
        "stocks_scanned": stocks_scanned,
        "rows_scanned": rows_scanned,
        "rows_flagged": rows_flagged,
        "flag_counts": dict(total_counts),
        "gaps_found": len(all_gaps),
        "gaps": all_gaps[:max_gaps_reported],
    }


def provenance_report() -> dict:
    """Powers the staff data-quality page and the CLI report."""
    total = PriceHistory.objects.count()
    by_source = _count_by(PriceHistory.objects.all(), "source")
    synthetic = PriceHistory.objects.filter(is_synthetic=True).count()
    unknown_source = PriceHistory.objects.filter(source=DataSource.UNKNOWN).count()
    flagged = PriceHistory.objects.exclude(quality_flags=[]).count()

    flag_counts: Counter = Counter()
    for flags in PriceHistory.objects.exclude(quality_flags=[]).values_list("quality_flags", flat=True).iterator():
        flag_counts.update(flags)

    freshness = {}
    for ex in (Exchange.DSE, Exchange.CSE):
        last_batch = ImportBatch.objects.filter(exchange=ex, finished_at__isnull=False, error="").order_by("-finished_at").first()
        last_row = PriceHistory.objects.filter(stock__exchange=ex).order_by("-date").first()
        freshness[ex] = {
            "last_successful_batch_at": last_batch.finished_at.isoformat() if last_batch else None,
            "last_successful_batch_source": last_batch.source if last_batch else None,
            "latest_price_date": last_row.date.isoformat() if last_row else None,
        }

    recent_batches = list(
        ImportBatch.objects.order_by("-started_at")[:20].values(
            "id", "source", "exchange", "started_at", "finished_at", "row_count", "stock_count", "error"
        )
    )

    return {
        "generated_at": timezone.now().isoformat(),
        "total_rows": total,
        "by_source": by_source,
        "synthetic_rows": synthetic,
        "unknown_provenance_rows": unknown_source,
        "flagged_rows": flagged,
        "flag_counts": dict(flag_counts),
        "freshness": freshness,
        "recent_batches": recent_batches,
    }


def _count_by(qs, field: str) -> dict:
    out: Counter = Counter()
    for val in qs.values_list(field, flat=True).iterator():
        out[val] += 1
    return dict(out)
