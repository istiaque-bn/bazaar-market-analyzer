"""
CSE (Chittagong Stock Exchange) fetcher.

Live quotes: scrape CSE current_price (bdshare fallback when available).
History: CSE public bulk Excel export (~2 years OHLC for all listed symbols).
"""
from __future__ import annotations

import io
import logging
from datetime import date, datetime, timedelta

import pandas as pd
import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.utils import timezone

from market.models import DataSource, Exchange, PriceHistory, Stock
from market.services.data_quality import create_import_batch, finish_import_batch, snapshot_revision_if_changed, validate_ohlcv
from market.services.dse_fetcher import (
    DEMO_SYMBOLS,
    _safe_float,
    _safe_int,
    generate_synthetic_history,
    save_history,
    upsert_live_quotes,
)

logger = logging.getLogger(__name__)

CSE_HOME = "https://www.cse.com.bd/"
CSE_HISTORICAL = "https://www.cse.com.bd/market/historicaldata"
CSE_BULK_DOWNLOAD = "https://www.cse.com.bd/market/data_download_company"
USER_AGENT = "BazaarMarketAnalyzer/1.0 (+educational)"


def _session(verify=None):
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


def _get(url: str, timeout: int = 25, session: requests.Session | None = None):
    sess = session or _session()
    return sess.get(url, timeout=timeout)


def _post(url: str, data: dict, timeout: int = 120, session: requests.Session | None = None):
    headers = {"Referer": CSE_HISTORICAL, "User-Agent": USER_AGENT}
    sess = session or _session()
    return sess.post(url, data=data, timeout=timeout, headers=headers)


def _fetch_csrf_token(session: requests.Session | None = None) -> str | None:
    resp = _get(CSE_HISTORICAL, timeout=30, session=session)
    if resp.status_code != 200:
        return None
    soup = BeautifulSoup(resp.text, "lxml")
    tag = soup.find("input", {"name": "csrf_cse_token"})
    return tag["value"] if tag and tag.get("value") else None


def _normalize_cse_bulk_df(df: pd.DataFrame) -> pd.DataFrame:
    """Map CSE company download columns to standard OHLCV frame."""
    if df is None or df.empty:
        return pd.DataFrame(columns=["date", "symbol", "open", "high", "low", "close", "volume", "value"])

    cols = {c.lower().strip(): c for c in df.columns}
    rename = {}
    for key, target in [
        ("trade_date", "date"),
        ("company_code", "symbol"),
        ("open_price", "open"),
        ("day_high", "high"),
        ("day_low", "low"),
        ("close_price", "close"),
        ("volume", "volume"),
        ("turnover", "value"),
    ]:
        if key in cols:
            rename[cols[key]] = target
    out = df.rename(columns=rename)
    required = {"date", "symbol", "close"}
    if not required.issubset(out.columns):
        return pd.DataFrame(columns=["date", "symbol", "open", "high", "low", "close", "volume", "value"])

    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.date
    out["symbol"] = out["symbol"].astype(str).str.strip().str.upper()
    for col in ("open", "high", "low", "close", "volume", "value"):
        if col not in out.columns:
            out[col] = out["close"] if col != "volume" else 0
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out["volume"] = out["volume"].fillna(0).astype(int)
    out["value"] = out["value"].fillna(0)
    for col in ("open", "high", "low"):
        out[col] = out[col].fillna(out["close"])
    out = out.dropna(subset=["date", "symbol", "close"])
    out = out[out["close"] > 0]
    return out.sort_values(["symbol", "date"]).drop_duplicates(["symbol", "date"]).reset_index(drop=True)


def fetch_cse_history_bulk(start: date, end: date) -> pd.DataFrame | None:
    """
    Download all CSE company OHLC rows for [start, end] via the public Excel export.

    CSE typically serves ~2 calendar years in this feed regardless of a longer request.
    """
    if end < start:
        return None
    session = _session()
    try:
        token = _fetch_csrf_token(session)
    except requests.exceptions.RequestException as exc:
        # Includes SSLError: fail safely rather than retrying without
        # verification. Previously-saved history is untouched by this path.
        logger.warning("CSE bulk history: request failed fetching CSRF token (%s)", type(exc).__name__)
        return None
    if not token:
        logger.warning("CSE bulk history: missing CSRF token")
        return None
    payload = {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "csrf_cse_token": token,
        "submit": "submit",
    }
    try:
        resp = _post(CSE_BULK_DOWNLOAD, payload, timeout=180, session=session)
    except requests.exceptions.RequestException as exc:
        logger.warning("CSE bulk history: request failed (%s)", type(exc).__name__)
        return None
    if resp.status_code != 200 or len(resp.content) < 1000:
        logger.warning("CSE bulk history failed: status=%s bytes=%s", resp.status_code, len(resp.content))
        return None
    ctype = (resp.headers.get("content-type") or "").lower()
    if "html" in ctype and b"[Content_Types]" not in resp.content[:200]:
        logger.warning("CSE bulk history returned HTML instead of Excel")
        return None
    try:
        raw = pd.read_excel(io.BytesIO(resp.content))
    except Exception as exc:
        logger.warning("CSE bulk history Excel parse failed: %s", exc)
        return None
    out = _normalize_cse_bulk_df(raw)
    if out.empty:
        return None
    mask = (out["date"] >= start) & (out["date"] <= end)
    return out.loc[mask].reset_index(drop=True)


def fetch_cse_history(
    code: str,
    start: date,
    end: date,
    bulk_df: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame | None, str]:
    """Return OHLC for one CSE symbol; reuses bulk_df when provided."""
    if end < start:
        return None, "invalid-range"
    code = code.strip().upper()
    if bulk_df is None:
        bulk_df = fetch_cse_history_bulk(start, end)
        source = "cse-bulk"
    else:
        source = "cse-bulk-cache"
    if bulk_df is None or bulk_df.empty:
        return None, "none"
    sub = bulk_df[bulk_df["symbol"] == code].copy()
    if sub.empty:
        return None, "none"
    sub = sub.sort_values("date").drop_duplicates("date")
    return sub[["date", "open", "high", "low", "close", "volume", "value"]], source


def fetch_cse_live_via_bdshare() -> pd.DataFrame | None:
    try:
        from bdshare import get_cse_current_trade_data
    except ImportError:
        # Installed bdshare builds are DSE-only and have never shipped a CSE
        # trade-data function — this is an expected, permanent gap, not a
        # transient failure, so don't warn-spam the log on every sync.
        logger.debug("bdshare has no CSE support in this version; using scrape fallback.")
        return None

    try:
        df = get_cse_current_trade_data()
        if df is None or df.empty:
            return None
        cols = {c.lower().strip(): c for c in df.columns}
        rename = {}
        for key, target in [
            ("symbol", "trading_code"),
            ("trading code", "trading_code"),
            ("ltp", "ltp"),
            ("high", "high"),
            ("low", "low"),
            ("close", "close"),
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
        logger.warning("bdshare CSE live fetch failed: %s", exc)
        return None


def fetch_cse_live_scrape() -> pd.DataFrame | None:
    """Best-effort scrape of CSE market data pages."""
    candidates = [
        "https://www.cse.com.bd/market/current_price",
        "https://www.cse.com.bd/market/price_list",
    ]
    for url in candidates:
        try:
            resp = _get(url, timeout=25)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "lxml")
            table = soup.find("table")
            if not table:
                continue
            rows = []
            for tr in table.find_all("tr")[1:]:
                cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                if len(cells) < 5:
                    continue
                code = cells[1].strip().upper() if len(cells) > 1 and cells[1] else cells[0].strip().upper()
                if not code or len(code) > 20 or code in ("SL.", "STOCK CODE"):
                    continue
                nums = [_safe_float(c) for c in cells[2:]]
                nums = [n for n in nums if n is not None]
                if not nums:
                    continue
                ltp = nums[0]
                rows.append(
                    {
                        "trading_code": code,
                        "ltp": ltp,
                        "high": nums[2] if len(nums) > 2 else ltp,
                        "low": nums[3] if len(nums) > 3 else ltp,
                        "close": ltp,
                        "change": nums[5] if len(nums) > 5 else 0,
                        "volume": int(nums[-1]) if nums[-1] and nums[-1] > 100 else 0,
                        "value": 0,
                        "ycp": nums[4] if len(nums) > 4 else ltp,
                    }
                )
            if rows:
                return pd.DataFrame(rows)
        except Exception as exc:
            logger.warning("CSE scrape %s failed: %s", url, exc)
    return None


def sync_cse_live() -> dict:
    batch = create_import_batch(DataSource.CSE_LIVE, exchange=Exchange.CSE)
    df = fetch_cse_live_via_bdshare()
    sub_source = "bdshare"
    if df is None:
        df = fetch_cse_live_scrape()
        sub_source = "scrape"
    if df is not None and not df.empty:
        try:
            count = upsert_live_quotes(df, Exchange.CSE, source=DataSource.CSE_LIVE, import_batch=batch)
        except Exception as exc:
            finish_import_batch(batch, error=str(exc), notes=f"sub_source={sub_source}")
            raise
        finish_import_batch(batch, row_count=count, notes=f"sub_source={sub_source}")
        return {"ok": True, "source": sub_source, "count": count}
    finish_import_batch(batch, error="No live CSE data available", notes=f"tried={sub_source}")

    # Mirror known symbols with slight price drift from DSE if available.
    # This is NOT real CSE data — tagged cse_mirror_fallback/is_synthetic
    # so it's excluded from live analysis/training/backtests by default.
    mirror_batch = create_import_batch(DataSource.CSE_MIRROR_FALLBACK, exchange=Exchange.CSE, note="mirrored from DSE last price, not a real CSE fetch")
    mirrored = 0
    now = timezone.now()
    try:
        for code, name, sector, group in DEMO_SYMBOLS:
            dse = Stock.objects.filter(exchange=Exchange.DSE, trading_code=code).first()
            stock, _ = Stock.objects.update_or_create(
                exchange=Exchange.CSE,
                trading_code=code,
                defaults={
                    "company_name": name,
                    "sector": sector,
                    "group": group,
                    "is_active": True,
                    "last_price": (dse.last_price * 0.998) if dse and dse.last_price else None,
                    "last_change_pct": dse.last_change_pct if dse else None,
                    "last_volume": int((dse.last_volume or 0) * 0.3) if dse else None,
                },
            )
            if stock.last_price:
                today = timezone.localdate()
                existing = PriceHistory.objects.filter(stock=stock, date=today).first()
                new_values = {
                    "open": stock.last_price,
                    "high": stock.last_price * 1.01,
                    "low": stock.last_price * 0.99,
                    "close": stock.last_price,
                    "volume": stock.last_volume or 0,
                }
                if existing is not None:
                    snapshot_revision_if_changed(existing, new_values, new_source=DataSource.CSE_MIRROR_FALLBACK, reason="cse_mirror_update")
                PriceHistory.objects.update_or_create(
                    stock=stock,
                    date=today,
                    defaults={
                        **new_values,
                        "source": DataSource.CSE_MIRROR_FALLBACK,
                        "is_synthetic": True,
                        "fetched_at": now,
                        "adjustment_status": "raw",
                        "import_batch": mirror_batch,
                        "quality_flags": validate_ohlcv(new_values["open"], new_values["high"], new_values["low"], new_values["close"], new_values["volume"]),
                    },
                )
                mirrored += 1
    except Exception as exc:
        finish_import_batch(mirror_batch, row_count=mirrored, error=str(exc))
        raise
    finish_import_batch(mirror_batch, row_count=mirrored, stock_count=mirrored)
    return {"ok": mirrored > 0, "source": "mirror", "count": mirrored}


def sync_cse_history(
    codes: list[str] | None = None,
    lookback_days: int | None = None,
    use_synthetic_fallback: bool = False,
    limit: int | None = None,
    _prefetched_bulk: pd.DataFrame | None = None,
) -> dict:
    """
    Fetch real CSE OHLC via the public bulk Excel export and merge into PriceHistory.

    Public CSE feeds typically expose ~2 years of day-end history.
    """
    lookback = lookback_days or settings.LOOKBACK_DAYS
    end = timezone.localdate()
    start = end - timedelta(days=lookback)

    if codes is None:
        qs = Stock.objects.filter(exchange=Exchange.CSE, is_active=True).values_list("trading_code", flat=True)
        if limit:
            qs = qs[:limit]
        codes = list(qs)

    bulk_df = _prefetched_bulk if _prefetched_bulk is not None else fetch_cse_history_bulk(start, end)
    source = "cse-bulk"
    if bulk_df is None or bulk_df.empty:
        if use_synthetic_fallback and codes:
            fallback_batch = create_import_batch(DataSource.SYNTHETIC_FALLBACK, exchange=Exchange.CSE, reason="CSE bulk download unavailable")
            filled = 0
            try:
                for i, code in enumerate(codes):
                    stock, _ = Stock.objects.get_or_create(exchange=Exchange.CSE, trading_code=code.upper())
                    if stock.prices.count() >= min(200, lookback // 2):
                        continue
                    df = generate_synthetic_history(f"CSE-{code}", days=min(260, lookback), seed=5000 + i)
                    save_history(stock, df, source=DataSource.SYNTHETIC_FALLBACK, import_batch=fallback_batch)
                    filled += 1
            except Exception as exc:
                finish_import_batch(fallback_batch, row_count=filled, error=str(exc))
                raise
            finish_import_batch(fallback_batch, row_count=filled, stock_count=filled)
            return {
                "ok": filled > 0,
                "filled": filled,
                "skipped": len(codes) - filled,
                "codes": len(codes),
                "source": "synthetic",
                "note": "CSE bulk download unavailable; used synthetic fallback",
            }
        return {
            "ok": 0,
            "failed_or_fallback": 0,
            "skipped": len(codes),
            "codes": len(codes),
            "lookback_days": lookback,
            "bars_saved": 0,
            "source": None,
            "note": "CSE bulk download failed",
        }

    # Ensure stocks exist for every symbol present in the bulk feed
    symbol_names = {}
    if "company_name" in bulk_df.columns:
        for sym, grp in bulk_df.groupby("symbol"):
            symbol_names[sym] = str(grp.iloc[-1].get("company_name") or sym)

    available = set(bulk_df["symbol"].unique())
    if not codes:
        codes = sorted(available)

    ok, fail, skipped = 0, 0, 0
    bars_saved = 0
    min_dates: list[date] = []
    max_dates: list[date] = []
    errors: list[str] = []
    batch = create_import_batch(DataSource.CSE_HISTORY, exchange=Exchange.CSE, lookback_days=lookback, codes_requested=len(codes))
    fallback_batch = None

    try:
        for i, code in enumerate(codes, start=1):
            code = code.upper()
            stock, _ = Stock.objects.get_or_create(
                exchange=Exchange.CSE,
                trading_code=code,
                defaults={"company_name": symbol_names.get(code, code), "is_active": True},
            )
            if not stock.is_active:
                stock.is_active = True
                stock.save(update_fields=["is_active", "updated_at"])

            df, row_source = fetch_cse_history(code, start, end, bulk_df=bulk_df)
            if df is None or df.empty:
                if use_synthetic_fallback and not stock.prices.exists():
                    if fallback_batch is None:
                        fallback_batch = create_import_batch(DataSource.SYNTHETIC_FALLBACK, exchange=Exchange.CSE, reason="real fetch unavailable for this code")
                    df = generate_synthetic_history(f"CSE-{code}", days=min(260, lookback), seed=5000 + i)
                    save_history(stock, df, source=DataSource.SYNTHETIC_FALLBACK, import_batch=fallback_batch)
                    fail += 1
                    errors.append(f"{code}: synthetic")
                else:
                    skipped += 1
                    errors.append(f"{code}: no history")
                continue
            n = save_history(stock, df, source=DataSource.CSE_HISTORY, import_batch=batch)
            bars_saved += int(n or 0)
            ok += 1
            try:
                min_dates.append(pd.to_datetime(df["date"]).min().date())
                max_dates.append(pd.to_datetime(df["date"]).max().date())
            except Exception:
                pass
            if i % 25 == 0 or i == len(codes):
                logger.info(
                    "CSE history progress %s/%s (ok=%s skipped=%s source=%s bars+=%s)",
                    i,
                    len(codes),
                    ok,
                    skipped,
                    row_source,
                    bars_saved,
                )
    except Exception as exc:
        finish_import_batch(batch, row_count=bars_saved, stock_count=ok, error=str(exc))
        if fallback_batch is not None:
            finish_import_batch(fallback_batch, row_count=fail, stock_count=fail, error=str(exc))
        raise
    finish_import_batch(batch, row_count=bars_saved, stock_count=ok)
    if fallback_batch is not None:
        finish_import_batch(fallback_batch, row_count=fail, stock_count=fail)

    bulk_min = bulk_df["date"].min()
    bulk_max = bulk_df["date"].max()
    return {
        "ok": ok,
        "failed_or_fallback": fail,
        "skipped": skipped,
        "codes": len(codes),
        "symbols_in_feed": len(available),
        "lookback_days": lookback,
        "bars_saved": bars_saved,
        "coverage_from": min(min_dates).isoformat() if min_dates else bulk_min.isoformat() if hasattr(bulk_min, "isoformat") else str(bulk_min),
        "coverage_to": max(max_dates).isoformat() if max_dates else bulk_max.isoformat() if hasattr(bulk_max, "isoformat") else str(bulk_max),
        "source": source,
        "note": (
            "Public CSE bulk export served up to ~5 years in testing; "
            "availability may vary by request window."
        ),
        "errors_sample": errors[:15],
    }
