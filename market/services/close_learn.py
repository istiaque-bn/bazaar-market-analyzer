"""
Next-day close learn loop.

After each trading session close:
  1. Forecast tomorrow's close for every active share (target = close).
  2. When tomorrow's bar arrives, settle: compare predicted_close vs actual close.
  3. Update global (+ per-stock liquid) return bias and RF model.

Precision upgrades:
  - Skill vs naive baseline (tomorrow close = today close)
  - Index / sector / breadth features
  - Per-stock EMA bias for top liquid names
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from django.conf import settings
from django.db.models import Avg
from django.utils import timezone
from sklearn.ensemble import RandomForestRegressor
from sklearn.model_selection import train_test_split

from market.models import CloseLearnState, NextDayCloseForecast, PriceHistory, Stock
from market.services.indicators import compute_indicators, prices_to_df
from market.services.market_hours import TRADING_WEEKDAYS

logger = logging.getLogger(__name__)

MODEL_PATH = Path(settings.CACHE_DIR) / "next_close_rf.pkl"
FEATURE_COLS = [
    "rsi_14",
    "macd_hist",
    "return_5d",
    "return_20d",
    "volatility_20",
    "volume_ratio",
    "dist_sma20",
    "dist_sma50",
    "index_ret_1d",
    "sector_ret_1d",
    "breadth",
    "rel_ret_1d",
]
TECH_COLS = [
    "rsi_14",
    "macd_hist",
    "return_5d",
    "return_20d",
    "volatility_20",
    "volume_ratio",
    "dist_sma20",
    "dist_sma50",
]
BIAS_EMA_ALPHA = 0.08
STOCK_BIAS_ALPHA = 0.12
MIN_HISTORY = 40
LIQUID_TOP_N = 80
STOCK_BIAS_MIN_SETTLES = 8

# Cache market/sector frames within a process run
_CONTEXT_CACHE: dict[str, pd.DataFrame] = {}


def next_trading_day(from_date: date) -> date:
    d = from_date
    for _ in range(8):
        d = d + timedelta(days=1)
        if d.weekday() in TRADING_WEEKDAYS:
            return d
    return from_date + timedelta(days=2)


def get_learn_state(key: str = "global") -> CloseLearnState:
    state, _ = CloseLearnState.objects.get_or_create(key=key)
    return state


def stock_bias_key(stock_id: int) -> str:
    return f"stock:{stock_id}"


def liquid_stock_ids(limit: int = LIQUID_TOP_N) -> set[int]:
    """Top names by recent average volume (liquidity proxy)."""
    qs = (
        PriceHistory.objects.filter(date__gte=timezone.localdate() - timedelta(days=45))
        .values("stock_id")
        .annotate(avg_vol=Avg("volume"))
        .order_by("-avg_vol")[:limit]
    )
    return {row["stock_id"] for row in qs}


def get_combined_bias(stock: Stock | None, liquid_ids: set[int] | None = None) -> tuple[float, float, float]:
    """Return (combined, global_bias, stock_bias)."""
    global_state = get_learn_state("global")
    g = float(global_state.return_bias or 0.0)
    s = 0.0
    if stock is not None:
        ids = liquid_ids if liquid_ids is not None else liquid_stock_ids()
        if stock.id in ids:
            st = get_learn_state(stock_bias_key(stock.id))
            if st.settled_count >= STOCK_BIAS_MIN_SETTLES:
                s = float(st.return_bias or 0.0)
    return g + s, g, s


def _clear_context_cache():
    _CONTEXT_CACHE.clear()


def build_exchange_context(exchange: str, start: date | None = None, end: date | None = None) -> pd.DataFrame:
    """
    Cross-sectional daily context for an exchange:
      index_ret_1d  — equal-weight mean stock return
      breadth       — share of stocks with positive return that day
    """
    exchange = (exchange or "DSE").upper()
    end = end or timezone.localdate()
    start = start or (end - timedelta(days=400))
    cache_key = f"ex|{exchange}|{start}|{end}"
    if cache_key in _CONTEXT_CACHE:
        return _CONTEXT_CACHE[cache_key]

    rows = list(
        PriceHistory.objects.filter(
            stock__exchange=exchange,
            stock__is_active=True,
            date__gte=start,
            date__lte=end,
        )
        .order_by("date")
        .values("date", "stock_id", "close", "stock__sector")
    )
    if not rows:
        empty = pd.DataFrame(columns=["date", "index_ret_1d", "breadth"])
        _CONTEXT_CACHE[cache_key] = empty
        return empty

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df = df.sort_values(["stock_id", "date"])
    df["ret"] = df.groupby("stock_id")["close"].pct_change()

    daily = (
        df.groupby("date")
        .agg(
            index_ret_1d=("ret", "mean"),
            breadth=("ret", lambda s: float((s.dropna() > 0).mean()) if s.dropna().size else 0.5),
        )
        .reset_index()
    )
    daily["index_ret_1d"] = daily["index_ret_1d"].fillna(0.0)
    daily["breadth"] = daily["breadth"].fillna(0.5)
    _CONTEXT_CACHE[cache_key] = daily
    return daily


def build_sector_context(exchange: str, sector: str, start: date | None = None, end: date | None = None) -> pd.DataFrame:
    """Equal-weight mean return of peers in the same sector."""
    exchange = (exchange or "DSE").upper()
    sector = (sector or "").strip() or "Other"
    end = end or timezone.localdate()
    start = start or (end - timedelta(days=400))
    cache_key = f"sec|{exchange}|{sector}|{start}|{end}"
    if cache_key in _CONTEXT_CACHE:
        return _CONTEXT_CACHE[cache_key]

    rows = list(
        PriceHistory.objects.filter(
            stock__exchange=exchange,
            stock__is_active=True,
            stock__sector=sector,
            date__gte=start,
            date__lte=end,
        )
        .order_by("date")
        .values("date", "stock_id", "close")
    )
    if not rows:
        empty = pd.DataFrame(columns=["date", "sector_ret_1d"])
        _CONTEXT_CACHE[cache_key] = empty
        return empty

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df = df.sort_values(["stock_id", "date"])
    df["ret"] = df.groupby("stock_id")["close"].pct_change()
    daily = df.groupby("date")["ret"].mean().rename("sector_ret_1d").reset_index()
    daily["sector_ret_1d"] = daily["sector_ret_1d"].fillna(0.0)
    _CONTEXT_CACHE[cache_key] = daily
    return daily


def _lookup_context(ctx: pd.DataFrame, on_date, col: str, default: float = 0.0) -> float:
    if ctx is None or ctx.empty:
        return default
    ts = pd.Timestamp(on_date).normalize()
    hit = ctx.loc[ctx["date"] == ts]
    if hit.empty:
        return default
    val = hit.iloc[0].get(col)
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return default
    return float(val)


def _tech_feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    data = compute_indicators(df)
    if data.empty:
        return pd.DataFrame()
    out = data.copy()
    out["volume_ratio"] = out["volume"] / out["volume_sma_20"].replace(0, np.nan)
    out["dist_sma20"] = out["close"] / out["sma_20"] - 1
    out["dist_sma50"] = out["close"] / out["sma_50"] - 1
    out["stock_ret_1d"] = out["close"].pct_change()
    return out


def _attach_market_features(
    tech: pd.DataFrame,
    exchange: str,
    sector: str,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    if tech.empty:
        return tech
    out = tech.copy()
    out["date"] = pd.to_datetime(out["date"]).dt.normalize()
    ex = build_exchange_context(exchange, start=start, end=end)
    sec = build_sector_context(exchange, sector, start=start, end=end)
    out = out.merge(ex[["date", "index_ret_1d", "breadth"]], on="date", how="left")
    out = out.merge(sec[["date", "sector_ret_1d"]], on="date", how="left")
    out["index_ret_1d"] = out["index_ret_1d"].fillna(0.0)
    out["sector_ret_1d"] = out["sector_ret_1d"].fillna(0.0)
    out["breadth"] = out["breadth"].fillna(0.5)
    out["rel_ret_1d"] = out["stock_ret_1d"].fillna(0.0) - out["index_ret_1d"]
    return out


def _feature_row(
    df: pd.DataFrame,
    exchange: str = "DSE",
    sector: str = "",
) -> dict | None:
    tech = _tech_feature_frame(df)
    if tech.empty or len(tech) < MIN_HISTORY:
        return None
    start = pd.Timestamp(tech["date"].iloc[0]).date()
    end = pd.Timestamp(tech["date"].iloc[-1]).date()
    framed = _attach_market_features(tech, exchange, sector, start=start, end=end)
    row = framed.iloc[-1]
    feats = {c: None if pd.isna(row.get(c)) else float(row[c]) for c in FEATURE_COLS}
    # Context cols default to 0/0.5 rather than rejecting the row
    for c, default in (("index_ret_1d", 0.0), ("sector_ret_1d", 0.0), ("breadth", 0.5), ("rel_ret_1d", 0.0)):
        if feats[c] is None:
            feats[c] = default
    if any(feats[c] is None for c in TECH_COLS):
        return None
    return feats


def _analogue_one_day_return(df: pd.DataFrame) -> tuple[float, float, int]:
    closes = df["close"].astype(float).values
    if len(closes) < MIN_HISTORY:
        return 0.0, 0.04, 0
    fwd = []
    for i in range(len(closes) - 1):
        if closes[i] <= 0:
            continue
        fwd.append(closes[i + 1] / closes[i] - 1)
    if len(fwd) < 10:
        rets = pd.Series(closes).pct_change().dropna()
        med = float(rets.tail(40).mean()) if len(rets) else 0.0
        spread = float(rets.tail(40).std() or 0.02)
        return med, spread, len(rets)
    arr = np.array(fwd)
    med = float(np.median(arr))
    spread = float(np.percentile(arr, 75) - np.percentile(arr, 25))
    return med, max(spread, 0.005), len(arr)


def _ml_one_day_return(feats: dict) -> float | None:
    if not MODEL_PATH.exists():
        return None
    try:
        bundle = joblib.load(MODEL_PATH)
        model = bundle["model"]
        cols = bundle.get("features", FEATURE_COLS)
        payload = {c: feats.get(c, 0.0) for c in cols}
        row = pd.DataFrame([payload])
        return float(model.predict(row)[0])
    except Exception as exc:
        logger.warning("next-close ML predict failed: %s", exc)
        return None


def forecast_next_close(
    df: pd.DataFrame,
    return_bias: float = 0.0,
    *,
    exchange: str = "DSE",
    sector: str = "",
) -> dict | None:
    """Predict next trading day's close; applies combined return bias correction."""
    if df is None or df.empty or len(df) < MIN_HISTORY:
        return None
    data = df.copy()
    data["date"] = pd.to_datetime(data["date"]).dt.normalize()
    data = data.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    last = data.iloc[-1]
    last_close = float(last["close"])
    if last_close <= 0:
        return None

    analogue_ret, spread, samples = _analogue_one_day_return(data)
    feats = _feature_row(data, exchange=exchange, sector=sector)
    ml_ret = _ml_one_day_return(feats) if feats else None

    if ml_ret is not None:
        raw_ret = 0.50 * analogue_ret + 0.50 * ml_ret
        method = "analogue+ml+ctx+bias"
    else:
        raw_ret = analogue_ret
        method = "analogue+ctx+bias"

    corrected = float(np.clip(raw_ret - return_bias, -0.12, 0.12))
    predicted_close = last_close * (1 + corrected)
    conf = float(np.clip(0.35 + min(samples, 80) / 160 - spread * 4, 0.15, 0.85))

    return {
        "last_close": round(last_close, 4),
        "predicted_close": round(predicted_close, 4),
        "predicted_return": round(corrected, 6),
        "raw_return": round(raw_ret, 6),
        "return_bias": round(return_bias, 6),
        "confidence": round(conf, 3),
        "method": method,
        "samples": samples,
        "features": feats or {},
    }


def compute_skill_metrics(limit: int = 8000) -> dict:
    """
    Compare model vs naive baseline (predicted return = 0 ⇒ tomorrow close = today close).
    Positive skill_vs_naive means model MAE_return is lower than baseline.
    """
    rows = list(
        NextDayCloseForecast.objects.filter(actual_close__isnull=False)
        .order_by("-target_date")
        .values_list("last_close", "predicted_close", "predicted_return", "actual_close")[:limit]
    )
    if not rows:
        return {
            "n": 0,
            "model_mae_return": None,
            "baseline_mae_return": None,
            "model_mape": None,
            "baseline_mape": None,
            "skill_vs_naive": None,
            "beats_naive_pct": None,
            "direction_hit_rate": None,
        }

    model_abs_ret = []
    base_abs_ret = []
    model_mape = []
    base_mape = []
    beats = 0
    dir_hits = dirs = 0

    for last_c, pred_c, pred_r, act_c in rows:
        if not last_c or not act_c:
            continue
        actual_ret = act_c / last_c - 1
        pred_r = float(pred_r or 0.0)
        m_err = abs(pred_r - actual_ret)
        b_err = abs(0.0 - actual_ret)  # naive return = 0
        model_abs_ret.append(m_err)
        base_abs_ret.append(b_err)
        if act_c:
            model_mape.append(abs(pred_c - act_c) / act_c * 100)
            base_mape.append(abs(last_c - act_c) / act_c * 100)
        if m_err < b_err - 1e-12:
            beats += 1
        if abs(actual_ret) > 1e-9 or abs(pred_r) > 1e-9:
            dirs += 1
            if (pred_r >= 0 and actual_ret >= 0) or (pred_r < 0 and actual_ret < 0):
                dir_hits += 1

    n = len(model_abs_ret)
    m_mae = float(np.mean(model_abs_ret)) if model_abs_ret else None
    b_mae = float(np.mean(base_abs_ret)) if base_abs_ret else None
    skill = None
    if m_mae is not None and b_mae and b_mae > 1e-12:
        skill = float(1.0 - m_mae / b_mae)

    return {
        "n": n,
        "model_mae_return": None if m_mae is None else round(m_mae, 6),
        "baseline_mae_return": None if b_mae is None else round(b_mae, 6),
        "model_mape": round(float(np.mean(model_mape)), 4) if model_mape else None,
        "baseline_mape": round(float(np.mean(base_mape)), 4) if base_mape else None,
        "skill_vs_naive": None if skill is None else round(skill, 4),
        "beats_naive_pct": round(beats / n, 4) if n else None,
        "direction_hit_rate": round(dir_hits / dirs, 4) if dirs else None,
    }


def generate_forecasts_for_as_of(as_of: date | None = None, limit: int | None = None) -> dict:
    """After close on `as_of`, write next-day close forecasts for active stocks."""
    as_of = as_of or timezone.localdate()
    target = next_trading_day(as_of)
    liquid = liquid_stock_ids()
    _clear_context_cache()

    qs = Stock.objects.filter(is_active=True).order_by("trading_code")
    if limit:
        qs = qs[:limit]

    created = updated = skipped = 0
    for stock in qs.iterator():
        bars = stock.prices.filter(date__lte=as_of).order_by("date")
        df = prices_to_df(bars)
        if df.empty or len(df) < MIN_HISTORY:
            skipped += 1
            continue
        last_date = pd.Timestamp(df.iloc[-1]["date"]).date()
        if last_date > as_of:
            df = df[pd.to_datetime(df["date"]).dt.date <= as_of]
        if len(df) < MIN_HISTORY:
            skipped += 1
            continue

        combined, g_bias, s_bias = get_combined_bias(stock, liquid)
        pred = forecast_next_close(
            df,
            return_bias=combined,
            exchange=stock.exchange,
            sector=stock.sector or "",
        )
        if not pred:
            skipped += 1
            continue

        _, was_created = NextDayCloseForecast.objects.update_or_create(
            stock=stock,
            target_date=target,
            defaults={
                "as_of": as_of,
                "last_close": pred["last_close"],
                "predicted_close": pred["predicted_close"],
                "predicted_return": pred["predicted_return"],
                "confidence": pred["confidence"],
                "method": pred["method"],
                "features": {
                    **pred["features"],
                    "raw_return": pred["raw_return"],
                    "return_bias": pred["return_bias"],
                    "global_bias": g_bias,
                    "stock_bias": s_bias,
                    "liquid": stock.id in liquid,
                    "analogue_samples": pred["samples"],
                },
            },
        )
        if was_created:
            created += 1
        else:
            updated += 1

    state = get_learn_state()
    state.last_forecast_at = timezone.now()
    state.save(update_fields=["last_forecast_at", "updated_at"])
    return {
        "ok": True,
        "as_of": as_of.isoformat(),
        "target_date": target.isoformat(),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "liquid_universe": len(liquid),
    }


def settle_due_forecasts(through_date: date | None = None) -> dict:
    """Fill actual close; update global + liquid per-stock bias; refresh skill metrics."""
    through_date = through_date or timezone.localdate()
    pending = (
        NextDayCloseForecast.objects.filter(actual_close__isnull=True, target_date__lte=through_date)
        .select_related("stock")
        .order_by("target_date")
    )
    settled = 0
    abs_errs: list[float] = []
    pct_errs: list[float] = []
    dir_hits = dir_n = 0

    state = get_learn_state()
    bias = float(state.return_bias or 0.0)
    liquid = liquid_stock_ids()

    for fc in pending.iterator():
        bar = PriceHistory.objects.filter(stock=fc.stock, date=fc.target_date).first()
        if not bar or not bar.close or fc.last_close <= 0:
            continue
        # Skip zero-volume / suspended-like bars from learning
        if bar.volume is not None and int(bar.volume) <= 0:
            continue

        actual = float(bar.close)
        actual_ret = actual / float(fc.last_close) - 1
        abs_err = abs(float(fc.predicted_close) - actual)
        pct_err = abs_err / actual * 100 if actual else None
        ret_err = float(fc.predicted_return) - actual_ret

        fc.actual_close = actual
        fc.abs_error = round(abs_err, 4)
        fc.pct_error = None if pct_err is None else round(pct_err, 4)
        fc.return_error = round(ret_err, 6)
        fc.settled_at = timezone.now()
        fc.save(update_fields=["actual_close", "abs_error", "pct_error", "return_error", "settled_at"])
        settled += 1
        abs_errs.append(abs_err)
        if pct_err is not None:
            pct_errs.append(pct_err)
        if abs(actual_ret) > 1e-9 or abs(fc.predicted_return) > 1e-9:
            dir_n += 1
            if (fc.predicted_return >= 0 and actual_ret >= 0) or (
                fc.predicted_return < 0 and actual_ret < 0
            ):
                dir_hits += 1

        bias = (1 - BIAS_EMA_ALPHA) * bias + BIAS_EMA_ALPHA * ret_err

        # Per-stock bias for liquid names
        if fc.stock_id in liquid:
            st = get_learn_state(stock_bias_key(fc.stock_id))
            sb = float(st.return_bias or 0.0)
            st.return_bias = (1 - STOCK_BIAS_ALPHA) * sb + STOCK_BIAS_ALPHA * ret_err
            st.settled_count = int(st.settled_count or 0) + 1
            st.last_settled_at = timezone.now()
            st.save(update_fields=["return_bias", "settled_count", "last_settled_at", "updated_at"])

    skill = compute_skill_metrics()
    if settled:
        batch_mae = float(np.mean(abs_errs)) if abs_errs else 0.0
        batch_mape = float(np.mean(pct_errs)) if pct_errs else 0.0
        n0 = state.settled_count
        n1 = n0 + settled
        if n0 <= 0:
            state.mae = batch_mae
            state.mape = batch_mape
            state.direction_hit_rate = (dir_hits / dir_n) if dir_n else 0.0
        else:
            w = settled / n1
            state.mae = (1 - w) * float(state.mae) + w * batch_mae
            state.mape = (1 - w) * float(state.mape) + w * batch_mape
            if dir_n:
                state.direction_hit_rate = (1 - w) * float(state.direction_hit_rate) + w * (dir_hits / dir_n)
        state.settled_count = n1
        state.return_bias = float(bias)
        state.last_settled_at = timezone.now()
        state.extras = {
            **(state.extras or {}),
            "last_batch_settled": settled,
            "last_batch_mae": round(batch_mae, 4),
            "last_batch_mape": round(batch_mape, 4),
            "skill": skill,
        }
        state.save()
    else:
        # Still refresh skill snapshot
        state.extras = {**(state.extras or {}), "skill": skill}
        state.save(update_fields=["extras", "updated_at"])

    return {
        "ok": True,
        "settled": settled,
        "return_bias": round(bias, 6),
        "mae": round(float(state.mae), 4),
        "mape": round(float(state.mape), 4),
        "direction_hit_rate": round(float(state.direction_hit_rate), 4),
        "settled_count": state.settled_count,
        "skill": skill,
    }


def train_next_close_model(limit_stocks: int = 80) -> dict:
    """Train RF: tech + index/sector/breadth → next-day close return."""
    _clear_context_cache()
    frames = []
    # Prefer liquid names for training signal quality
    liquid = liquid_stock_ids(limit=max(limit_stocks, LIQUID_TOP_N))
    stocks = list(Stock.objects.filter(id__in=liquid, is_active=True)[:limit_stocks])
    if len(stocks) < 15:
        stocks = list(Stock.objects.filter(is_active=True).order_by("-last_volume")[:limit_stocks])

    for stock in stocks:
        df = prices_to_df(stock.prices.all())
        if len(df) < 80:
            continue
        tech = _tech_feature_frame(df)
        if tech.empty:
            continue
        start = pd.Timestamp(tech["date"].iloc[0]).date()
        end = pd.Timestamp(tech["date"].iloc[-1]).date()
        out = _attach_market_features(tech, stock.exchange, stock.sector or "", start=start, end=end)
        out["fwd_ret_1"] = out["close"].shift(-1) / out["close"] - 1
        frames.append(out[FEATURE_COLS + ["fwd_ret_1"]].dropna())

    if not frames:
        return {"ok": False, "error": "Not enough data to train next-close model"}
    data = pd.concat(frames, ignore_index=True)
    data = data.replace([np.inf, -np.inf], np.nan).dropna()
    if len(data) < 300:
        return {"ok": False, "error": f"Need ~300 rows, got {len(data)}"}

    X = data[FEATURE_COLS]
    y = data["fwd_ret_1"]
    # Clip extreme returns / features so RF stays stable
    y = y.clip(-0.2, 0.2)
    X = X.clip(lower=-50, upper=50)
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
    model = RandomForestRegressor(
        n_estimators=140,
        max_depth=7,
        min_samples_leaf=10,
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X_train, y_train)
    pred = model.predict(X_test)
    mae = float(np.mean(np.abs(pred - y_test)))
    hit = float(np.mean((pred >= 0) == (y_test.values >= 0)))
    # vs naive on same test fold
    baseline_mae = float(np.mean(np.abs(y_test.values)))
    skill = float(1.0 - mae / baseline_mae) if baseline_mae > 1e-12 else 0.0

    joblib.dump(
        {
            "model": model,
            "features": FEATURE_COLS,
            "mae": mae,
            "direction_hit": hit,
            "skill_vs_naive": skill,
        },
        MODEL_PATH,
    )
    state = get_learn_state()
    state.last_trained_at = timezone.now()
    state.extras = {
        **(state.extras or {}),
        "ml_mae_return": round(mae, 6),
        "ml_direction_hit": round(hit, 4),
        "ml_skill_vs_naive": round(skill, 4),
        "ml_train_rows": len(X_train),
        "ml_test_rows": len(X_test),
        "feature_cols": FEATURE_COLS,
    }
    state.save(update_fields=["last_trained_at", "extras", "updated_at"])
    return {
        "ok": True,
        "mae_return": round(mae, 6),
        "direction_hit": round(hit, 4),
        "skill_vs_naive": round(skill, 4),
        "train_rows": len(X_train),
        "test_rows": len(X_test),
        "path": str(MODEL_PATH),
    }


def run_close_learn_cycle(as_of: date | None = None, train: bool = True) -> dict:
    as_of = as_of or timezone.localdate()
    settle = settle_due_forecasts(through_date=as_of)
    train_info = None
    if train:
        if settle.get("settled", 0) > 0 or not MODEL_PATH.exists():
            try:
                train_info = train_next_close_model()
            except Exception as exc:
                logger.exception("next-close train failed")
                train_info = {"ok": False, "error": str(exc)}
    forecasts = generate_forecasts_for_as_of(as_of=as_of)
    return {"settle": settle, "train": train_info, "forecasts": forecasts, "status": learn_status()}


def learn_status() -> dict:
    state = get_learn_state()
    pending = NextDayCloseForecast.objects.filter(actual_close__isnull=True).count()
    latest = (
        NextDayCloseForecast.objects.filter(actual_close__isnull=False)
        .select_related("stock")
        .order_by("-settled_at")
        .first()
    )
    skill = (state.extras or {}).get("skill") or compute_skill_metrics()
    if skill and skill.get("skill_vs_naive") is not None:
        skill = {**skill, "skill_pct": round(float(skill["skill_vs_naive"]) * 100, 2)}
    stock_bias_n = CloseLearnState.objects.filter(key__startswith="stock:").filter(settled_count__gte=STOCK_BIAS_MIN_SETTLES).count()
    return {
        "return_bias": round(float(state.return_bias or 0), 6),
        "mae": round(float(state.mae or 0), 4),
        "mape": round(float(state.mape or 0), 4),
        "direction_hit_rate": round(float(state.direction_hit_rate or 0), 4),
        "settled_count": state.settled_count,
        "pending_forecasts": pending,
        "last_forecast_at": state.last_forecast_at.isoformat() if state.last_forecast_at else None,
        "last_settled_at": state.last_settled_at.isoformat() if state.last_settled_at else None,
        "last_trained_at": state.last_trained_at.isoformat() if state.last_trained_at else None,
        "ml_model": MODEL_PATH.exists(),
        "stock_bias_active": stock_bias_n,
        "liquid_universe": len(liquid_stock_ids()),
        "skill": skill,
        "extras": state.extras or {},
        "latest_settled": (
            {
                "stock": latest.stock.trading_code,
                "target_date": latest.target_date.isoformat(),
                "predicted": latest.predicted_close,
                "actual": latest.actual_close,
                "pct_error": latest.pct_error,
            }
            if latest
            else None
        ),
    }


def backfill_learn_from_history(lookback_days: int = 60, limit_stocks: int = 40) -> dict:
    """Simulate predict→settle on recent history to seed bias / skill / ML."""
    end = timezone.localdate()
    hist_start = end - timedelta(days=lookback_days + 280)
    settle_from = end - timedelta(days=lookback_days)
    created = settled = 0
    state = get_learn_state()
    bias = float(state.return_bias or 0.0)
    liquid = liquid_stock_ids(limit=max(limit_stocks, LIQUID_TOP_N))
    _clear_context_cache()

    # Prefer liquid names for backfill quality
    stocks = list(Stock.objects.filter(id__in=liquid, is_active=True).order_by("trading_code")[:limit_stocks])
    if len(stocks) < max(8, limit_stocks // 2):
        stocks = list(Stock.objects.filter(is_active=True).order_by("-last_volume")[:limit_stocks])

    for stock in stocks:
        df = prices_to_df(stock.prices.filter(date__gte=hist_start, date__lte=end))
        if len(df) < MIN_HISTORY + 5:
            continue
        df = df.reset_index(drop=True)
        dates = [pd.Timestamp(d).date() for d in df["date"]]
        stock_bias = 0.0
        stock_settles = 0
        for i in range(MIN_HISTORY - 1, len(df) - 1):
            as_of = dates[i]
            target = dates[i + 1]
            if target < settle_from:
                continue
            hist = df.iloc[: i + 1]
            combined = bias + (stock_bias if stock.id in liquid and stock_settles >= STOCK_BIAS_MIN_SETTLES else 0.0)
            pred = forecast_next_close(
                hist,
                return_bias=combined,
                exchange=stock.exchange,
                sector=stock.sector or "",
            )
            if not pred:
                continue
            fc, was_created = NextDayCloseForecast.objects.update_or_create(
                stock=stock,
                target_date=target,
                defaults={
                    "as_of": as_of,
                    "last_close": pred["last_close"],
                    "predicted_close": pred["predicted_close"],
                    "predicted_return": pred["predicted_return"],
                    "confidence": pred["confidence"],
                    "method": pred["method"] + "+backfill",
                    "features": pred["features"],
                    "actual_close": None,
                    "abs_error": None,
                    "pct_error": None,
                    "return_error": None,
                    "settled_at": None,
                },
            )
            if was_created:
                created += 1
            actual = float(df.iloc[i + 1]["close"])
            vol = float(df.iloc[i + 1].get("volume") or 0)
            if vol <= 0:
                continue
            actual_ret = actual / pred["last_close"] - 1
            abs_err = abs(pred["predicted_close"] - actual)
            pct_err = abs_err / actual * 100 if actual else 0.0
            ret_err = pred["predicted_return"] - actual_ret
            fc.actual_close = actual
            fc.abs_error = round(abs_err, 4)
            fc.pct_error = round(pct_err, 4)
            fc.return_error = round(ret_err, 6)
            fc.settled_at = timezone.now()
            fc.save()
            settled += 1
            bias = (1 - BIAS_EMA_ALPHA) * bias + BIAS_EMA_ALPHA * ret_err
            if stock.id in liquid:
                stock_bias = (1 - STOCK_BIAS_ALPHA) * stock_bias + STOCK_BIAS_ALPHA * ret_err
                stock_settles += 1

        if stock.id in liquid and stock_settles:
            st = get_learn_state(stock_bias_key(stock.id))
            st.return_bias = stock_bias
            st.settled_count = stock_settles
            st.last_settled_at = timezone.now()
            st.save()

    skill = compute_skill_metrics()
    qs = NextDayCloseForecast.objects.filter(actual_close__isnull=False)
    n = qs.count()
    if n:
        abs_vals = list(qs.exclude(abs_error=None).values_list("abs_error", flat=True)[:5000])
        pct_vals = list(qs.exclude(pct_error=None).values_list("pct_error", flat=True)[:5000])
        state.settled_count = n
        state.return_bias = bias
        state.mae = float(np.mean(abs_vals)) if abs_vals else 0.0
        state.mape = float(np.mean(pct_vals)) if pct_vals else 0.0
        state.direction_hit_rate = float(skill.get("direction_hit_rate") or 0.0)
        state.last_settled_at = timezone.now()
        state.extras = {**(state.extras or {}), "skill": skill}
        state.save()

    train_info = train_next_close_model(limit_stocks=limit_stocks)
    return {
        "ok": True,
        "forecasts_written": created,
        "settled": settled,
        "return_bias": round(bias, 6),
        "skill": skill,
        "train": train_info,
        "status": learn_status(),
    }
