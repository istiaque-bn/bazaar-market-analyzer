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

from market.models import Exchange, MarketSnapshot, PriceHistory, Stock, StockGroup

logger = logging.getLogger(__name__)

DSE_LATEST = "https://www.dsebd.org/latest_share_price_scroll_l.php"
DSE_HIST = "https://www.dsebd.org/day_end_archive.php"
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
    """GET with SSL verify, then insecure fallback (common on local macOS Python)."""
    headers = {"User-Agent": USER_AGENT}
    try:
        return _session().get(url, timeout=timeout)
    except requests.exceptions.SSLError:
        logger.warning("SSL verify failed for %s — retrying without verification", url)
        # Prefer bare requests.get: Session+REQUESTS_CA_BUNDLE can still fail verify=False on some Pythons
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return requests.get(url, headers=headers, timeout=timeout, verify=False)


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
        _ensure_ssl_patch()
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


def _ensure_ssl_patch():
    """Patch requests so bdshare works on machines with broken CA stores."""
    import requests as _req
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    if getattr(_req.Session, "_bazaar_ssl_patch", False):
        return
    _orig = _req.Session.request

    def _patched(self, method, url, **kwargs):
        kwargs.setdefault("timeout", 45)
        try:
            return _orig(self, method, url, **kwargs)
        except (_req.exceptions.SSLError, _req.exceptions.ConnectionError) as exc:
            if "SSL" not in type(exc).__name__ and "SSL" not in str(exc):
                raise
            kwargs["verify"] = False
            return _orig(self, method, url, **kwargs)

    _req.Session.request = _patched
    _req.Session._bazaar_ssl_patch = True


def fetch_dse_history_via_bdshare(code: str, start: date, end: date) -> pd.DataFrame | None:
    try:
        _ensure_ssl_patch()
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
def upsert_live_quotes(df: pd.DataFrame, exchange: str = Exchange.DSE) -> int:
    """
    Persist the latest successful live fetch into Stock + today's PriceHistory.

    - Always overwrites Stock.last_* with this fetch (last successful fetch wins).
    - Upserts PriceHistory for *today* only — past history is never deleted.
    - If today's bar already exists, keeps the earlier open and expands high/low.
    """
    count = 0
    today = timezone.localdate()
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

        existing = PriceHistory.objects.filter(stock=stock, date=today).first()
        if existing:
            # Later fetch wins on close/LTP/volume; preserve session open; expand range
            existing.close = ltp
            existing.volume = volume
            existing.value = _safe_float(row.get("value"), existing.value) or existing.value
            existing.high = max(existing.high or ltp, high, ltp)
            existing.low = min(existing.low or ltp, low, ltp) if (existing.low or low) else ltp
            # Keep first open of the day unless it was missing/zero
            if not existing.open:
                existing.open = open_px
            existing.save()
        else:
            PriceHistory.objects.create(
                stock=stock,
                date=today,
                open=open_px,
                high=max(high, ltp),
                low=min(low, ltp),
                close=ltp,
                volume=volume,
                value=_safe_float(row.get("value"), 0) or 0,
            )
        count += 1
    return count


def save_history(stock: Stock, df: pd.DataFrame, replace_all: bool = False) -> int:
    """
    Persist ~1y OHLC. By default MERGES rows (upsert) so a later live fetch
    for *today* is not wiped when history is (re)loaded.

    If replace_all=True, deletes all bars for the stock first (rare/admin use).
    """
    if df is None or df.empty:
        return 0
    today = timezone.localdate()
    if replace_all:
        PriceHistory.objects.filter(stock=stock).delete()

    saved = 0
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
        # Do not clobber today's live bar with an older/stale archive close
        # if we already have a today row (live upsert wins for today).
        if d == today and PriceHistory.objects.filter(stock=stock, date=today).exists():
            # Still refresh OHLC from history if history is for today and looks complete;
            # prefer max high / min low / history close only when no live preference —
            # keep existing close (live) but allow history to fill missing open/high/low.
            existing = PriceHistory.objects.get(stock=stock, date=today)
            existing.open = existing.open or defaults["open"]
            existing.high = max(existing.high or 0, defaults["high"], existing.close or 0)
            existing.low = min(
                x for x in (existing.low, defaults["low"], existing.close) if x and x > 0
            )
            existing.save()
            saved += 1
        else:
            PriceHistory.objects.update_or_create(stock=stock, date=d, defaults=defaults)
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
    """Seed DSE+CSE demo stocks with 1y synthetic history."""
    created = 0
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
            save_history(stock, df)
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
    return {"stocks": Stock.objects.count(), "prices": PriceHistory.objects.count()}


def sync_dse_live() -> dict:
    df = fetch_dse_live_via_bdshare()
    source = "bdshare"
    if df is None:
        df = fetch_dse_live_via_scrape()
        source = "scrape"
    if df is None:
        return {"ok": False, "source": None, "count": 0, "error": "No live DSE data available"}
    count = upsert_live_quotes(df, Exchange.DSE)
    return {"ok": True, "source": source, "count": count}


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
    for i, code in enumerate(codes, start=1):
        stock, _ = Stock.objects.get_or_create(exchange=Exchange.DSE, trading_code=code.upper())
        if code.upper() in prefetched_map:
            df, source = prefetched_map[code.upper()]
        else:
            df, source = fetch_dse_history(code, start, end)
        if df is None or df.empty:
            if use_synthetic_fallback and not stock.prices.exists():
                df = generate_synthetic_history(code, days=min(260, lookback))
                save_history(stock, df)
                fail += 1
                errors.append(f"{code}: synthetic")
            else:
                skipped += 1
                errors.append(f"{code}: no history")
            continue
        n = save_history(stock, df)
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
                source,
                bars_saved,
            )
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
