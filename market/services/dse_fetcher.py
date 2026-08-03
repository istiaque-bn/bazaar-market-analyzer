"""
DSE data fetcher — live quotes + ~1 year historical OHLC.

Uses bdshare when available; falls back to dsebd.org HTML scraping.
Also supports synthetic demo data when offline.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from market.models import SYNTHETIC_SOURCES, DataSource, Exchange, MarketSnapshot, PriceHistory, Stock, StockGroup
from market.services.data_quality import (
    blocks_synthetic_overwrite,
    create_import_batch,
    finish_import_batch,
    snapshot_revision_if_changed,
    validate_ohlcv,
)

logger = logging.getLogger(__name__)

DSE_LATEST = "https://www.dsebd.org/latest_share_price_scroll_l.php"
DSE_HIST = "https://www.dsebd.org/day_end_archive.php"
DSE_LATEST_PE = "https://www.dsebd.org/latest_PE.php"
USER_AGENT = "BazaarMarketAnalyzer/1.0 (+educational; respectful rate limits)"


def _session(verify: bool | str | None = None) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    if verify is None:
        if not getattr(settings, "DSE_SSL_VERIFY", True):
            verify = False
        else:
            try:
                import certifi

                verify = certifi.where()
            except Exception:
                verify = True
    s.verify = verify
    if verify is False:
        try:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass
    return s


def _get(url: str, timeout: int = 30):
    """GET with certificate verification. Raises on SSL failure — callers
    already catch broadly and log, so a bad cert fails the fetch instead of
    silently retrying without verification."""
    return _session().get(url, timeout=timeout)


def _safe_float(val: Any, default: float | None = None) -> float | None:
    if val is None:
        return default
    try:
        text = str(val).replace(",", "").strip()
        if text in ("", "-", "N/A", "n/a", "--"):
            return default
        return float(text)
    except (TypeError, ValueError):
        return default


def _safe_int(val: Any, default: int = 0) -> int:
    f = _safe_float(val)
    return int(f) if f is not None else default


def fetch_dse_live_via_bdshare() -> pd.DataFrame | None:
    try:
        from bdshare import get_current_trade_data

        df = get_current_trade_data()
        if df is None or df.empty:
            return None
        cols = {c.lower().strip(): c for c in df.columns}
        rename = {}
        for key, target in [
            ("trading code", "trading_code"),
            ("ltp", "ltp"),
            ("high", "high"),
            ("low", "low"),
            ("closep", "close"),
            ("ycp", "ycp"),
            ("change", "change"),
            ("trade", "trades"),
            ("value", "value"),
            ("volume", "volume"),
        ]:
            if key in cols:
                rename[cols[key]] = target
        df = df.rename(columns=rename)
        if "trading_code" not in df.columns:
            return None
        return df
    except Exception as exc:
        logger.warning("bdshare live fetch failed: %s", exc)
        return None


def fetch_dse_live_via_scrape() -> pd.DataFrame | None:
    try:
        resp = _get(DSE_LATEST, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        table = soup.select_one("table.shares-table") or soup.find(
            "table", class_=lambda c: c and "shares-table" in c
        )
        if not table:
            # Fallback: table whose header mentions TRADING CODE
            for candidate in soup.find_all("table"):
                head = candidate.find("tr")
                if head and "TRADING CODE" in head.get_text(" ", strip=True).upper():
                    table = candidate
                    break
        if not table:
            return None
        rows = []
        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(cells) < 10:
                continue
            # #, CODE, LTP, HIGH, LOW, CLOSEP, YCP, CHANGE, TRADE, VALUE, VOLUME
            code = cells[1].strip().upper()
            if not code or code == "#":
                continue
            rows.append(
                {
                    "trading_code": code,
                    "ltp": _safe_float(cells[2]),
                    "high": _safe_float(cells[3]),
                    "low": _safe_float(cells[4]),
                    "close": _safe_float(cells[5]),
                    "ycp": _safe_float(cells[6]),
                    "change": _safe_float(cells[7]),
                    "trades": _safe_int(cells[8]),
                    "value": _safe_float(cells[9]),
                    "volume": _safe_int(cells[10] if len(cells) > 10 else 0),
                }
            )
        if not rows:
            return None
        return pd.DataFrame(rows)
    except Exception as exc:
        logger.warning("DSE scrape failed: %s", exc)
        return None


def fetch_dse_pe_ratios() -> pd.DataFrame | None:
    """Scrape dsebd.org's bulk "Trailing P/E" table — one request covers
    every listed DSE company. Not sourced via bdshare's get_latest_pe():
    that helper slices off exactly the "Trailing P/E" column (keeps only
    the mostly-"n/a" P/E 1-6 variants), so this parses the page directly
    instead. Columns: #, Trade Code, Close Price, YCP, P/E 1-6*, Trailing
    P/E."""
    try:
        resp = _get(DSE_LATEST_PE, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        table = soup.select_one("table.shares-table") or soup.find(
            "table", class_=lambda c: c and "shares-table" in c
        )
        if not table:
            for candidate in soup.find_all("table"):
                head = candidate.find("tr")
                if head and "TRADE CODE" in head.get_text(" ", strip=True).upper():
                    table = candidate
                    break
        if not table:
            return None
        rows = []
        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(cells) < 11:
                continue
            code = cells[1].strip().upper()
            if not code or code == "#":
                continue
            pe = _safe_float(cells[10])
            if pe is None or pe <= 0:
                continue
            rows.append({"trading_code": code, "pe_ratio": pe})
        if not rows:
            return None
        return pd.DataFrame(rows)
    except Exception as exc:
        logger.warning("DSE latest_PE scrape failed: %s", exc)
        return None


def sync_dse_pe_ratios() -> dict:
    """Update Stock.pe_ratio for every matching active DSE stock from the
    bulk trailing-P/E table. A live snapshot only (no history is kept),
    so it's safe for display but must NOT be used as a walk-forward ML
    feature as-is — that would need a genuine daily time series to avoid
    leaking a stock's future P/E into historical training rows."""
    df = fetch_dse_pe_ratios()
    if df is None or df.empty:
        return {"ok": False, "error": "No P/E data available", "updated": 0}

    pe_by_code = dict(zip(df["trading_code"], df["pe_ratio"]))
    stocks = list(Stock.objects.filter(exchange=Exchange.DSE, is_active=True, trading_code__in=pe_by_code.keys()))
    for stock in stocks:
        stock.pe_ratio = pe_by_code[stock.trading_code]
    updated = Stock.objects.bulk_update(stocks, ["pe_ratio"], batch_size=200)
    return {"ok": True, "updated": len(stocks), "matched_of_scraped": len(stocks), "scraped_rows": len(df)}


def fetch_dse_history_via_bdshare(code: str, start: date, end: date) -> pd.DataFrame | None:
    try:
        from bdshare import get_hist_data, get_basic_hist_data

        df = None
        try:
            df = get_basic_hist_data(start.isoformat(), end.isoformat(), code)
        except Exception:
            df = None
        if df is None or (hasattr(df, "empty") and df.empty):
            df = get_hist_data(start.isoformat(), end.isoformat(), code)
        if df is None or df.empty:
            return None
        # Reset index if date is index
        if "date" not in [c.lower() for c in df.columns] and df.index.name:
            df = df.reset_index()
        cols = {c.lower().strip(): c for c in df.columns}
        mapping = {}
        for key, target in [
            ("date", "date"),
            ("open", "open"),
            ("openp", "open"),
            ("high", "high"),
            ("low", "low"),
            ("close", "close"),
            ("closep", "close"),
            ("volume", "volume"),
            ("value", "value"),
        ]:
            if key in cols and target not in mapping.values():
                mapping[cols[key]] = target
        df = df.rename(columns=mapping)
        if "date" not in df.columns or "close" not in df.columns:
            return None
        df["date"] = pd.to_datetime(df["date"]).dt.date
        for col in ("open", "high", "low", "close", "volume", "value"):
            if col not in df.columns:
                df[col] = df["close"] if col != "volume" else 0
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        return df.sort_values("date").drop_duplicates("date")
    except Exception as exc:
        logger.warning("bdshare history %s failed: %s", code, exc)
        return None


def fetch_dse_history_via_archive(code: str, start: date, end: date) -> pd.DataFrame | None:
    """Fallback: scrape DSE day-end archive for one instrument (chunked by caller if needed)."""
    try:
        url = (
            "https://www.dsebd.org/day_end_archive.php"
            f"?startDate={start.isoformat()}&endDate={end.isoformat()}"
            f"&inst={code}&archive=data"
        )
        resp = _get(url, timeout=60)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "lxml")
        table = soup.find("table", class_="shares-table")
        if table is None:
            for candidate in soup.find_all("table"):
                head = candidate.find("tr")
                if not head:
                    continue
                labels = head.get_text(" ", strip=True).upper()
                if "DATE" in labels and ("CLOSEP" in labels or "CLOSE" in labels) and "TRADING" in labels:
                    table = candidate
                    break
        if table is None:
            return None

        header_cells = table.find("tr").find_all(["th", "td"])
        headers = [th.get_text(" ", strip=True).lower().replace("*", "") for th in header_cells]
        # Normalize header names
        def col(*names):
            for n in names:
                if n in headers:
                    return headers.index(n)
            return None

        i_date = col("date")
        i_open = col("openp", "open")
        i_high = col("high")
        i_low = col("low")
        i_close = col("closep", "close")
        i_vol = col("volume")
        i_val = col("value (mn)", "value")
        if i_date is None or i_close is None:
            return None

        rows = []
        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(cells) <= max(i_date, i_close):
                continue
            if "no day end" in cells[0].lower():
                return None
            try:
                d = pd.to_datetime(cells[i_date]).date()
            except Exception:
                continue
            if d < start or d > end:
                continue
            close = _safe_float(cells[i_close])
            if close is None:
                continue
            open_p = _safe_float(cells[i_open]) if i_open is not None and i_open < len(cells) else close
            high = _safe_float(cells[i_high]) if i_high is not None and i_high < len(cells) else close
            low = _safe_float(cells[i_low]) if i_low is not None and i_low < len(cells) else close
            volume = _safe_int(cells[i_vol]) if i_vol is not None and i_vol < len(cells) else 0
            value = _safe_float(cells[i_val]) if i_val is not None and i_val < len(cells) else 0
            rows.append(
                {
                    "date": d,
                    "open": open_p or close,
                    "high": high or close,
                    "low": low or close,
                    "close": close,
                    "volume": volume,
                    "value": value or 0,
                }
            )
        if not rows:
            return None
        return pd.DataFrame(rows).sort_values("date").drop_duplicates("date")
    except Exception as exc:
        logger.warning("archive history %s failed: %s", code, exc)
        return None


def fetch_dse_history(code: str, start: date, end: date) -> tuple[pd.DataFrame | None, str]:
    """
    Fetch OHLC for [start, end].

    Public DSE feeds typically return at most ~2 years even when a longer
    lookback is requested. We ask for the full window once via bdshare, then
    merge a recent archive window to fill gaps.
    """
    if end < start:
        return None, "invalid-range"

    chunks: list[pd.DataFrame] = []
    source_used = None

    df_b = fetch_dse_history_via_bdshare(code, start, end)
    if df_b is not None and not df_b.empty:
        chunks.append(df_b)
        source_used = "bdshare"

    # Recent archive window (DSE day-end pages are more reliable near-term)
    arch_start = max(start, end - timedelta(days=400))
    df_a = fetch_dse_history_via_archive(code, arch_start, end)
    if df_a is not None and not df_a.empty:
        chunks.append(df_a)
        source_used = "mixed" if source_used else "archive"

    if not chunks:
        # Last resort: try archive over a longer recent window in two halves
        mid = end - timedelta(days=365)
        for a, b in ((max(start, end - timedelta(days=730)), mid), (mid + timedelta(days=1), end)):
            if a > b:
                continue
            part = fetch_dse_history_via_archive(code, a, b)
            if part is not None and not part.empty:
                chunks.append(part)
                source_used = "archive"

    if not chunks:
        return None, "none"
    out = (
        pd.concat(chunks, ignore_index=True)
        .drop_duplicates("date")
        .sort_values("date")
        .reset_index(drop=True)
    )
    return out, source_used or "mixed"

DEMO_SYMBOLS = [
    ("GP", "Grameenphone Ltd", "Telecommunications", "A"),
    ("SQURPHARMA", "Square Pharmaceuticals", "Pharmaceuticals", "A"),
    ("BATBC", "British American Tobacco BD", "Food & Allied", "A"),
    ("BRACBANK", "BRAC Bank", "Bank", "A"),
    ("BXPHARMA", "Beximco Pharmaceuticals", "Pharmaceuticals", "A"),
    ("WALTONHIL", "Walton Hi-Tech Industries", "Engineering", "A"),
    ("CITYBANK", "City Bank", "Bank", "A"),
    ("MARICO", "Marico Bangladesh", "Food & Allied", "A"),
    ("RENATA", "Renata Limited", "Pharmaceuticals", "A"),
    ("ISLAMIBANK", "Islami Bank Bangladesh", "Bank", "A"),
    ("LHBL", "LafargeHolcim Bangladesh", "Cement", "A"),
    ("OLYMPIC", "Olympic Industries", "Food & Allied", "A"),
    ("SINGERBD", "Singer Bangladesh", "Engineering", "A"),
    ("UPGDCL", "United Power Generation", "Fuel & Power", "A"),
    ("BEACONPHAR", "Beacon Pharmaceuticals", "Pharmaceuticals", "B"),
]


def generate_synthetic_history(code: str, days: int = 260, seed: int | None = None) -> pd.DataFrame:
    """Realistic-looking random walk for offline demos / tests."""
    rng = np.random.default_rng(seed if seed is not None else abs(hash(code)) % (2**32))
    base = float(rng.uniform(40, 350))
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=days)
    rets = rng.normal(0.0004, 0.018, size=days)
    # Inject mild trends / mean reversion patches
    rets[60:90] += 0.004
    rets[150:180] -= 0.003
    close = base * np.cumprod(1 + rets)
    high = close * (1 + rng.uniform(0.002, 0.025, size=days))
    low = close * (1 - rng.uniform(0.002, 0.025, size=days))
    open_ = close * (1 + rng.normal(0, 0.008, size=days))
    volume = rng.integers(50_000, 2_000_000, size=days)
    return pd.DataFrame(
        {
            "date": [d.date() for d in dates],
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "value": close * volume / 1000,
        }
    )


@transaction.atomic
def upsert_live_quotes(
    df: pd.DataFrame,
    exchange: str = Exchange.DSE,
    source: str | None = None,
    import_batch=None,
) -> int:
    """
    Persist the latest successful live fetch into Stock + today's PriceHistory.

    - Always overwrites Stock.last_* with this fetch (last successful fetch wins).
    - Upserts PriceHistory for *today* only — past history is never deleted.
    - If today's bar already exists, keeps the earlier open and expands high/low.
    - Tags every write with `source`/`fetched_at`/`import_batch`; snapshots
      the prior values to PriceHistoryRevision before any material change.
    - On a day the market is actually closed (weekend/holiday), Stock.last_*
      is still refreshed (keeps the ticker showing the last known price) but
      no PriceHistory row is written — DSE/CSE's "live quote" endpoint keeps
      serving the last real close with no "market closed" signal, so writing
      it under today's date would fabricate a duplicate bar for a day that
      never traded (see market/services/trading_calendar.py).
    """
    from market.services.trading_calendar import closure_reason

    if source is None:
        source = DataSource.DSE_LIVE if exchange == Exchange.DSE else DataSource.CSE_LIVE
    count = 0
    today = timezone.localdate()
    now = timezone.now()
    is_trading_day_today = closure_reason(today) is None
    for _, row in df.iterrows():
        code = str(row.get("trading_code", "")).strip().upper()
        if not code or code == "NAN":
            continue
        ltp = _safe_float(row.get("ltp") or row.get("close"), 0) or 0
        change = _safe_float(row.get("change"), 0) or 0
        volume = _safe_int(row.get("volume"), 0)
        high = _safe_float(row.get("high"), ltp) or ltp
        low = _safe_float(row.get("low"), ltp) or ltp
        # Prefer true open if present; else prior close (ycp) as session open estimate
        open_px = _safe_float(row.get("open") or row.get("openp") or row.get("ycp"), ltp) or ltp

        stock, _ = Stock.objects.update_or_create(
            exchange=exchange,
            trading_code=code,
            defaults={
                "last_price": ltp if ltp > 0 else None,
                "last_change_pct": change,
                "last_volume": volume,
                "is_active": True,
            },
        )
        if ltp <= 0:
            count += 1
            continue
        if not is_trading_day_today:
            count += 1
            continue

        existing = PriceHistory.objects.filter(stock=stock, date=today).first()
        if existing:
            if blocks_synthetic_overwrite(existing, source):
                logger.warning("Skipping synthetic overwrite of confirmed-real row %s/%s", stock.trading_code, today)
                count += 1
                continue
            new_close = ltp
            new_volume = volume
            new_high = max(existing.high or ltp, high, ltp)
            new_low = min(existing.low or ltp, low, ltp) if (existing.low or low) else ltp
            snapshot_revision_if_changed(
                existing,
                {"open": existing.open, "high": new_high, "low": new_low, "close": new_close, "volume": new_volume},
                new_source=source,
                reason="live_quote_update",
            )
            # Later fetch wins on close/LTP/volume; preserve session open; expand range
            existing.close = new_close
            existing.volume = new_volume
            existing.value = _safe_float(row.get("value"), existing.value) or existing.value
            existing.high = new_high
            existing.low = new_low
            # Keep first open of the day unless it was missing/zero
            if not existing.open:
                existing.open = open_px
            existing.source = source
            existing.is_synthetic = source in SYNTHETIC_SOURCES
            existing.fetched_at = now
            # We know this write is raw/unadjusted (no CA data source exists at all —
            # see Phase 5), so a freshly-written row is never "unknown" adjustment.
            existing.adjustment_status = "raw"
            existing.import_batch = import_batch
            existing.quality_flags = validate_ohlcv(existing.open, existing.high, existing.low, existing.close, existing.volume)
            existing.save()
        else:
            open_v, high_v, low_v, close_v = open_px, max(high, ltp), min(low, ltp), ltp
            PriceHistory.objects.create(
                stock=stock,
                date=today,
                open=open_v,
                high=high_v,
                low=low_v,
                close=close_v,
                volume=volume,
                value=_safe_float(row.get("value"), 0) or 0,
                source=source,
                is_synthetic=source in SYNTHETIC_SOURCES,
                fetched_at=now,
                adjustment_status="raw",
                import_batch=import_batch,
                quality_flags=validate_ohlcv(open_v, high_v, low_v, close_v, volume),
            )
        count += 1
    return count


def save_history(
    stock: Stock,
    df: pd.DataFrame,
    replace_all: bool = False,
    source: str = DataSource.UNKNOWN,
    import_batch=None,
) -> int:
    """
    Persist ~1y OHLC. By default MERGES rows (upsert) so a later live fetch
    for *today* is not wiped when history is (re)loaded.

    If replace_all=True, deletes all bars for the stock first (rare/admin use).

    Every write is tagged with `source`/`fetched_at`/`import_batch` and
    validated (flags stored, never blocks the write). A synthetic `source`
    is refused if it would overwrite a row that already has a confirmed
    real source — see blocks_synthetic_overwrite().
    """
    if df is None or df.empty:
        return 0
    today = timezone.localdate()
    now = timezone.now()
    is_synthetic = source in SYNTHETIC_SOURCES
    if replace_all:
        PriceHistory.objects.filter(stock=stock).delete()

    saved = 0
    skipped_protected = 0
    newest_close = None
    newest_volume = 0
    newest_date = None
    for _, row in df.iterrows():
        d = row["date"]
        if isinstance(d, datetime):
            d = d.date()
        close = float(row["close"])
        volume = int(row.get("volume") or 0)
        defaults = {
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": close,
            "volume": volume,
            "value": float(row.get("value") or 0),
        }
        existing_row = PriceHistory.objects.filter(stock=stock, date=d).first()
        if blocks_synthetic_overwrite(existing_row, source):
            skipped_protected += 1
            continue

        # Do not clobber today's live bar with an older/stale archive close
        # if we already have a today row (live upsert wins for today).
        if d == today and existing_row is not None:
            # Still refresh OHLC from history if history is for today and looks complete;
            # prefer max high / min low / history close only when no live preference —
            # keep existing close (live) but allow history to fill missing open/high/low.
            new_high = max(existing_row.high or 0, defaults["high"], existing_row.close or 0)
            new_low = min(x for x in (existing_row.low, defaults["low"], existing_row.close) if x and x > 0)
            new_open = existing_row.open or defaults["open"]
            snapshot_revision_if_changed(
                existing_row,
                {"open": new_open, "high": new_high, "low": new_low, "close": existing_row.close, "volume": existing_row.volume},
                new_source=source,
                reason="history_merge_today",
            )
            existing_row.open = new_open
            existing_row.high = new_high
            existing_row.low = new_low
            existing_row.fetched_at = existing_row.fetched_at or now
            existing_row.quality_flags = validate_ohlcv(existing_row.open, existing_row.high, existing_row.low, existing_row.close, existing_row.volume)
            # Deliberately NOT re-tagging source/is_synthetic here: the row's
            # authoritative close/volume still come from whatever wrote them
            # (live), so its provenance stays attributed to that, not to
            # this history merge which only fills in open/high/low bounds.
            existing_row.save()
            saved += 1
        else:
            if existing_row is not None:
                snapshot_revision_if_changed(existing_row, defaults, new_source=source, reason="history_upsert")
            PriceHistory.objects.update_or_create(
                stock=stock,
                date=d,
                defaults={
                    **defaults,
                    "source": source,
                    "is_synthetic": is_synthetic,
                    "fetched_at": now,
                    "adjustment_status": "raw",
                    "import_batch": import_batch,
                    "quality_flags": validate_ohlcv(defaults["open"], defaults["high"], defaults["low"], defaults["close"], defaults["volume"]),
                },
            )
            saved += 1
        if newest_date is None or d >= newest_date:
            newest_date = d
            newest_close = close
            newest_volume = volume

    # Update stock last_* only if history newest is today or stock has no live price yet
    if newest_close is not None:
        if newest_date == today or stock.last_price is None:
            stock.last_price = newest_close
            stock.last_volume = newest_volume
            stock.save(update_fields=["last_price", "last_volume", "updated_at"])
    return saved


def seed_demo_universe(days: int = 260) -> dict:
    """Seed DSE+CSE demo stocks with 1y synthetic history. Always tagged
    source=synthetic_demo/is_synthetic=True — excluded from live analysis,
    training and backtests by default (PriceHistory.objects.live())."""
    batch = create_import_batch(DataSource.SYNTHETIC_DEMO, days=days, symbols=len(DEMO_SYMBOLS))
    created = 0
    try:
        for i, (code, name, sector, group) in enumerate(DEMO_SYMBOLS):
            for exchange in (Exchange.DSE, Exchange.CSE):
                stock, was_created = Stock.objects.update_or_create(
                    exchange=exchange,
                    trading_code=code if exchange == Exchange.DSE else f"{code}",
                    defaults={
                        "company_name": name,
                        "sector": sector,
                        "group": group if group in StockGroup.values else StockGroup.UNKNOWN,
                        "is_active": True,
                    },
                )
                df = generate_synthetic_history(f"{exchange}-{code}", days=days, seed=1000 + i * 10 + (0 if exchange == Exchange.DSE else 1))
                save_history(stock, df, source=DataSource.SYNTHETIC_DEMO, import_batch=batch)
                created += 1 if was_created else 0
        # Market snapshots
        for exchange in (Exchange.DSE, Exchange.CSE):
            MarketSnapshot.objects.update_or_create(
                exchange=exchange,
                as_of=timezone.localdate(),
                defaults={
                    "index_value": 6200 if exchange == Exchange.DSE else 18500,
                    "index_change_pct": 0.35,
                    "advancers": 120,
                    "decliners": 95,
                    "unchanged": 40,
                    "notes": "Demo snapshot",
                },
            )
    except Exception as exc:
        finish_import_batch(batch, error=str(exc))
        raise
    finish_import_batch(batch, row_count=PriceHistory.objects.filter(import_batch=batch).count(), stock_count=len(DEMO_SYMBOLS) * 2)
    return {"stocks": Stock.objects.count(), "prices": PriceHistory.objects.count(), "import_batch_id": batch.id}


def sync_dse_live() -> dict:
    batch = create_import_batch(DataSource.DSE_LIVE, exchange=Exchange.DSE)
    df = fetch_dse_live_via_bdshare()
    sub_source = "bdshare"
    if df is None:
        df = fetch_dse_live_via_scrape()
        sub_source = "scrape"
    if df is None:
        finish_import_batch(batch, error="No live DSE data available", notes=f"tried={sub_source}")
        return {"ok": False, "source": None, "count": 0, "error": "No live DSE data available"}
    try:
        count = upsert_live_quotes(df, Exchange.DSE, source=DataSource.DSE_LIVE, import_batch=batch)
    except Exception as exc:
        finish_import_batch(batch, error=str(exc), notes=f"sub_source={sub_source}")
        raise
    finish_import_batch(batch, row_count=count, notes=f"sub_source={sub_source}")
    return {"ok": True, "source": sub_source, "count": count}


def sync_dse_history(
    codes: list[str] | None = None,
    lookback_days: int | None = None,
    use_synthetic_fallback: bool = False,
    limit: int | None = None,
    _prefetched: list[tuple[str, pd.DataFrame | None, str]] | None = None,
) -> dict:
    lookback = lookback_days or settings.LOOKBACK_DAYS
    end = timezone.localdate()
    start = end - timedelta(days=lookback)
    if codes is None:
        qs = Stock.objects.filter(exchange=Exchange.DSE, is_active=True).values_list("trading_code", flat=True)
        if limit:
            qs = qs[:limit]
        codes = list(qs)
        if not codes:
            codes = [c for c, *_ in DEMO_SYMBOLS]
    ok, fail, skipped = 0, 0, 0
    bars_saved = 0
    min_dates: list[date] = []
    max_dates: list[date] = []
    errors: list[str] = []
    prefetched_map = {c.upper(): (df, src) for c, df, src in (_prefetched or [])}
    batch = create_import_batch(DataSource.DSE_HISTORY, exchange=Exchange.DSE, lookback_days=lookback, codes_requested=len(codes))
    fallback_batch = None
    try:
        for i, code in enumerate(codes, start=1):
            stock, _ = Stock.objects.get_or_create(exchange=Exchange.DSE, trading_code=code.upper())
            if code.upper() in prefetched_map:
                df, sub_source = prefetched_map[code.upper()]
            else:
                df, sub_source = fetch_dse_history(code, start, end)
            if df is None or df.empty:
                if use_synthetic_fallback and not stock.prices.exists():
                    if fallback_batch is None:
                        fallback_batch = create_import_batch(DataSource.SYNTHETIC_FALLBACK, exchange=Exchange.DSE, reason="real fetch unavailable")
                    df = generate_synthetic_history(code, days=min(260, lookback))
                    save_history(stock, df, source=DataSource.SYNTHETIC_FALLBACK, import_batch=fallback_batch)
                    fail += 1
                    errors.append(f"{code}: synthetic")
                else:
                    skipped += 1
                    errors.append(f"{code}: no history")
                continue
            n = save_history(stock, df, source=DataSource.DSE_HISTORY, import_batch=batch)
            bars_saved += int(n or 0)
            ok += 1
            try:
                min_dates.append(pd.to_datetime(df["date"]).min().date())
                max_dates.append(pd.to_datetime(df["date"]).max().date())
            except Exception:
                pass
            if i % 10 == 0 or i == len(codes):
                logger.info(
                    "DSE history progress %s/%s (ok=%s skipped=%s source=%s bars+=%s)",
                    i,
                    len(codes),
                    ok,
                    skipped,
                    sub_source,
                    bars_saved,
                )
    except Exception as exc:
        finish_import_batch(batch, row_count=bars_saved, stock_count=ok, error=str(exc))
        if fallback_batch is not None:
            finish_import_batch(fallback_batch, error=str(exc))
        raise
    finish_import_batch(batch, row_count=bars_saved, stock_count=ok)
    if fallback_batch is not None:
        finish_import_batch(fallback_batch, row_count=fail, stock_count=fail)
    return {
        "ok": ok,
        "failed_or_fallback": fail,
        "skipped": skipped,
        "codes": len(codes),
        "lookback_days": lookback,
        "bars_saved": bars_saved,
        "coverage_from": min(min_dates).isoformat() if min_dates else None,
        "coverage_to": max(max_dates).isoformat() if max_dates else None,
        "note": (
            "Public DSE/bdshare feeds typically expose ~2 years of day-end history; "
            "requested lookback may exceed available source data."
        ),
        "errors_sample": errors[:15],
    }
