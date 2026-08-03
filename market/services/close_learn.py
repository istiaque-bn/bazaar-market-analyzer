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
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor

from market.models import CloseLearnState, Exchange, NextDayCloseForecast, PriceHistory, Stock
from market.services.indicators import compute_indicators, prices_to_df
from market.services.market_hours import TRADING_WEEKDAYS
from market.services.ml_training import (
    active_model_version,
    apply_imputer,
    backup_existing_model,
    fit_median_imputer,
    new_version_tag,
    record_model_version,
    regression_metrics,
    regression_skill,
    validate_chronological,
    walk_forward_folds,
)

logger = logging.getLogger(__name__)

MODEL_NAME = "next_close_rf"
CACHE_DIR = Path(settings.CACHE_DIR)
MODEL_PATH = CACHE_DIR / "next_close_rf.pkl"  # combined-scope deployment
MODEL_PATH_BY_EXCHANGE = {
    Exchange.DSE: CACHE_DIR / "next_close_rf_DSE.pkl",
    Exchange.CSE: CACHE_DIR / "next_close_rf_CSE.pkl",
}
STATUS_ACTIVE = "active"
STATUS_EXPERIMENTAL = "experimental"
EMBARGO_CALENDAR_DAYS = 3  # >= 1 Sun-Thu trading day, covers the Thu->Sun weekend gap
MIN_PANEL_ROWS = 300
MIN_FOLD_TRAIN_ROWS = 150
MIN_FOLD_TEST_ROWS = 30
MIN_ROWS_PER_EXCHANGE_TO_POOL = 300
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
    from market.services.trading_calendar import closure_reason

    d = from_date
    # 15 days is generous headroom over the longest known closure cluster
    # (a 7-day Eid break butted up against a weekend).
    for _ in range(15):
        d = d + timedelta(days=1)
        if d.weekday() in TRADING_WEEKDAYS and closure_reason(d) is None:
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
        PriceHistory.objects.live()
        .filter(date__gte=timezone.localdate() - timedelta(days=45))
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
        PriceHistory.objects.live()
        .filter(
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
        PriceHistory.objects.live()
        .filter(
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


def _load_next_close_bundle(model_path: Path) -> dict | None:
    if not model_path.exists():
        return None
    try:
        return joblib.load(model_path)
    except Exception as exc:
        logger.warning("Failed to load next-close ML model %s: %s", model_path, exc)
        return None


def _is_deployable(bundle: dict, exchange_scope: str) -> bool:
    """The pickle's own "status" is a train-time snapshot; the
    MLModelVersion row is the live source of truth — it's what
    _maybe_downgrade_on_live_skill() flips post-hoc if the model starts
    missing live settlements without a retrain having happened yet."""
    if bundle.get("status") != STATUS_ACTIVE:
        return False
    active = active_model_version(MODEL_NAME, exchange_scope=exchange_scope)
    return active is not None and active.version == bundle.get("version")


def load_next_close_model(exchange: str | None = None) -> dict | None:
    """Prefer a combined-scope model if active; else fall back to a
    per-exchange model if that one is active. Fails closed (returns None)
    for missing/unrecognized status, matching market.services.ml_model's
    gate — an unverified or non-positive-skill model must never
    influence the next-close forecast."""
    combined = _load_next_close_bundle(MODEL_PATH)
    if combined is not None and _is_deployable(combined, "combined"):
        return combined
    if exchange and exchange in MODEL_PATH_BY_EXCHANGE:
        per_exchange = _load_next_close_bundle(MODEL_PATH_BY_EXCHANGE[exchange])
        if per_exchange is not None and _is_deployable(per_exchange, exchange):
            return per_exchange
    return None


def _ml_one_day_return(feats: dict, exchange: str | None = None) -> float | None:
    """Zero-inflated two-stage prediction: P(next-day return != 0) times a
    magnitude regressed only on historical mover days. A plain continuous
    regressor loses to the "predict no change" baseline on thin names that
    often go a whole session with an unchanged close — see
    _walk_forward_evaluate_next_close's docstring. Older single-model
    bundles (key "model") are still honored for backward compatibility."""
    bundle = load_next_close_model(exchange=exchange)
    if bundle is None:
        return None
    try:
        cols = bundle.get("features", FEATURE_COLS)
        payload = {c: feats.get(c, 0.0) for c in cols}
        row = pd.DataFrame([payload])
        imputer = bundle.get("imputer")
        if imputer is not None:
            row = apply_imputer(imputer, row)
        if "classifier" in bundle and "regressor" in bundle:
            p_move = float(bundle["classifier"].predict_proba(row)[0, 1])
            magnitude = float(bundle["regressor"].predict(row)[0])
            return p_move * magnitude
        return float(bundle["model"].predict(row)[0])
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
    ml_ret = _ml_one_day_return(feats, exchange=exchange) if feats else None

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


MIN_LIVE_SETTLES_FOR_GATE = 50


def _maybe_downgrade_on_live_skill(skill: dict) -> None:
    """Deployment gate, part 2: a model can look fine at train-time
    walk-forward evaluation and still degrade once it's actually forecasting
    live, unseen sessions. Once enough forecasts have genuinely settled
    against real closes, if live skill vs. the naive (zero-return) baseline
    has gone non-positive, flip every currently-active next_close_rf
    MLModelVersion row to inactive so _ml_one_day_return() stops using it —
    without requiring a manual retrain to notice the regression."""
    if not skill or skill.get("n", 0) < MIN_LIVE_SETTLES_FOR_GATE:
        return
    live_skill = skill.get("skill_vs_naive")
    if live_skill is None or live_skill > 0:
        return
    from market.models import MLModelVersion

    downgraded = MLModelVersion.objects.filter(model_name=MODEL_NAME, is_active=True).update(
        is_active=False,
        status=STATUS_EXPERIMENTAL,
    )
    if downgraded:
        logger.warning(
            "next_close_rf: live skill_vs_naive=%.4f over n=%d settled forecasts is non-positive; "
            "downgraded %d active model version(s) to experimental.",
            live_skill,
            skill.get("n", 0),
            downgraded,
        )


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
        bars = stock.prices.live().filter(date__lte=as_of).order_by("date")
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
    _maybe_downgrade_on_live_skill(skill)
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


def _build_next_close_panel(exchange: str, limit_stocks: int = 80) -> pd.DataFrame:
    """One row per (stock, date) with tech+context features, fwd_ret_1
    label and bookkeeping columns, for a single exchange. Each stock's
    history is chronologically validated before feature construction.
    Note: fwd_ret_1 = close.shift(-1)/close - 1 is NaN for the final bar
    of each stock (unknown next close) — the trailing .dropna() already
    correctly drops those rows rather than mislabeling them."""
    liquid = liquid_stock_ids(limit=max(limit_stocks, LIQUID_TOP_N))
    stocks = list(Stock.objects.filter(id__in=liquid, is_active=True, exchange=exchange)[:limit_stocks])
    if len(stocks) < 15:
        stocks = list(Stock.objects.filter(is_active=True, exchange=exchange).order_by("-last_volume")[:limit_stocks])

    frames = []
    for stock in stocks:
        df = prices_to_df(stock.prices.live())
        if len(df) < 80:
            continue
        df = validate_chronological(df, label=f"{stock.exchange}:{stock.trading_code}")
        tech = _tech_feature_frame(df)
        if tech.empty:
            continue
        start = pd.Timestamp(tech["date"].iloc[0]).date()
        end = pd.Timestamp(tech["date"].iloc[-1]).date()
        out = _attach_market_features(tech, stock.exchange, stock.sector or "", start=start, end=end)
        out["fwd_ret_1"] = out["close"].shift(-1) / out["close"] - 1
        cols = out[["date", "stock_ret_1d"] + FEATURE_COLS + ["fwd_ret_1"]].replace([np.inf, -np.inf], np.nan).dropna()
        if cols.empty:
            continue
        cols = cols.copy()
        cols["exchange"] = stock.exchange
        cols["stock_id"] = stock.id
        frames.append(cols)

    if not frames:
        return pd.DataFrame()
    panel = pd.concat(frames, ignore_index=True)
    panel["date"] = pd.to_datetime(panel["date"])
    return panel.sort_values("date", kind="mergesort").reset_index(drop=True)


ZERO_RETURN_EPS = 1e-9  # a next-day return this close to 0 is an unchanged close, not a rounding artifact


def _fit_zero_inflated_next_close(X_train_i: pd.DataFrame, y_train: np.ndarray):
    """Two-stage model: classify whether the close moves at all tomorrow,
    then regress a magnitude using only historical mover days, and
    multiply. A plain regressor loses to the zero_return baseline on thin
    names that often post an unchanged close for a whole session — no
    continuous model naturally lands on an exact 0.0, so it pays an error
    on every one of those rows while the naive baseline pays none. See
    forecast comparison in the 2026-08-03 next-close skill investigation:
    this flipped DSE's out-of-sample skill_vs_naive from -0.007 to +0.004."""
    move_train = (np.abs(y_train) > ZERO_RETURN_EPS).astype(int)
    classifier = RandomForestClassifier(
        n_estimators=140, max_depth=7, min_samples_leaf=10, random_state=42, n_jobs=-1, class_weight="balanced"
    )
    classifier.fit(X_train_i, move_train)

    mover_mask = move_train == 1
    regressor = RandomForestRegressor(n_estimators=140, max_depth=7, min_samples_leaf=10, random_state=42, n_jobs=-1)
    regressor.fit(X_train_i[mover_mask], y_train[mover_mask])
    return classifier, regressor


def _predict_zero_inflated(classifier, regressor, X_i: pd.DataFrame) -> np.ndarray:
    proba = classifier.predict_proba(X_i)
    if 1 in classifier.classes_:
        move_idx = list(classifier.classes_).index(1)
        p_move = proba[:, move_idx]
    else:
        # Every training row fell in one class (e.g. a small/synthetic
        # fold with no movers at all) — predict_proba only has one
        # column then. If that lone class is "no move" (0), there's
        # nothing to predict a magnitude for.
        p_move = np.zeros(len(X_i))
    magnitude = regressor.predict(X_i)
    return p_move * magnitude


def _walk_forward_evaluate_next_close(panel: pd.DataFrame, n_folds: int = 5) -> dict:
    """Chronological walk-forward CV with an embargo gap >= the 1-day
    label horizon. Baselines: zero-return (predict no change — the
    "previous close" naive forecast), persistence (predict yesterday's
    realized 1-day return continues) and simple-market (predict the
    train fold's mean realized return). A true "majority-class" baseline
    doesn't apply to this continuous regression target; it's intentionally
    not fabricated here (see README's Phase 4 notes)."""
    if panel.empty:
        return {"ok": False, "error": "no data"}

    folds = walk_forward_folds(panel["date"], n_folds=n_folds, embargo_days=EMBARGO_CALENDAR_DAYS)
    if not folds:
        return {"ok": False, "error": "not enough distinct trading dates for walk-forward folds"}

    fold_rows = []
    model_metrics_list = []
    baseline_metrics_list: dict[str, list[dict]] = {"zero_return": [], "persistence": [], "simple_market": []}

    for f in folds:
        train = panel.loc[panel["date"] <= f.train_end]
        test = panel.loc[(panel["date"] >= f.test_start) & (panel["date"] <= f.test_end)]
        if len(train) < MIN_FOLD_TRAIN_ROWS or len(test) < MIN_FOLD_TEST_ROWS:
            continue

        X_train = train[FEATURE_COLS].clip(lower=-50, upper=50)
        y_train = train["fwd_ret_1"].clip(-0.2, 0.2).to_numpy()
        X_test = test[FEATURE_COLS].clip(lower=-50, upper=50)
        y_test = test["fwd_ret_1"].clip(-0.2, 0.2).to_numpy()

        imputer = fit_median_imputer(X_train)  # preprocessing fit on the train fold only
        X_train_i = apply_imputer(imputer, X_train)
        X_test_i = apply_imputer(imputer, X_test)

        classifier, regressor = _fit_zero_inflated_next_close(X_train_i, y_train)
        pred = _predict_zero_inflated(classifier, regressor, X_test_i)

        m_metrics = regression_metrics(y_test, pred)
        model_metrics_list.append(m_metrics)

        zero_pred = np.zeros(len(y_test))
        pers_pred = test["stock_ret_1d"].fillna(0.0).to_numpy()
        mkt_pred = np.full(len(y_test), float(np.mean(y_train)))

        baseline_metrics_list["zero_return"].append(regression_metrics(y_test, zero_pred))
        baseline_metrics_list["persistence"].append(regression_metrics(y_test, pers_pred))
        baseline_metrics_list["simple_market"].append(regression_metrics(y_test, mkt_pred))

        fold_rows.append(
            {
                "fold": f.fold,
                "train_end": f.train_end.date().isoformat(),
                "test_start": f.test_start.date().isoformat(),
                "test_end": f.test_end.date().isoformat(),
                "train_rows": int(len(train)),
                "test_rows": int(len(test)),
                "metrics": m_metrics,
            }
        )

    if not fold_rows:
        return {"ok": False, "error": "no fold had enough rows after the embargo gap"}

    def _agg(dicts: list[dict], key: str):
        vals = [d[key] for d in dicts if d.get(key) is not None]
        return round(float(np.mean(vals)), 6) if vals else None

    metric_keys = ("mae", "mape", "direction_hit_rate")
    agg_model = {k: _agg(model_metrics_list, k) for k in metric_keys}
    agg_model["n"] = int(sum(m["n"] for m in model_metrics_list))
    agg_baselines = {name: {k: _agg(lst, k) for k in metric_keys} for name, lst in baseline_metrics_list.items()}

    skill = {name: regression_skill(agg_model, agg_baselines[name]) for name in agg_baselines}

    return {
        "ok": True,
        "n_folds_used": len(fold_rows),
        "folds": fold_rows,
        "model_metrics": agg_model,
        "baseline_metrics": agg_baselines,
        "skill_vs_baseline": skill,
        # zero-return ("predict no change from last close") is the naive
        # reference used for the deployment gate, matching the live skill
        # metric already tracked in compute_skill_metrics().
        "skill_vs_naive": skill.get("zero_return"),
    }


def _justify_combining_next_close(dse_eval: dict, cse_eval: dict) -> tuple[bool, str]:
    if not dse_eval.get("ok") or not cse_eval.get("ok"):
        return False, "one exchange had too little data to evaluate separately, so pooling can't be justified against it"
    dse_n, cse_n = dse_eval["model_metrics"]["n"], cse_eval["model_metrics"]["n"]
    if dse_n < MIN_ROWS_PER_EXCHANGE_TO_POOL or cse_n < MIN_ROWS_PER_EXCHANGE_TO_POOL:
        return False, f"insufficient out-of-sample rows to trust pooling (DSE={dse_n}, CSE={cse_n}, need >={MIN_ROWS_PER_EXCHANGE_TO_POOL} each)"
    dse_skill, cse_skill = dse_eval.get("skill_vs_naive"), cse_eval.get("skill_vs_naive")
    if dse_skill is None or cse_skill is None:
        return False, "walk-forward skill undefined for one exchange"
    if (dse_skill > 0) != (cse_skill > 0):
        return False, f"one exchange clears the deployment gate and the other doesn't (DSE={dse_skill}, CSE={cse_skill}); pooling would average away a real per-exchange win"
    if abs(dse_skill - cse_skill) > 0.15:
        return False, f"DSE/CSE walk-forward skill diverge too much to justify one pooled model (DSE={dse_skill}, CSE={cse_skill})"
    return True, f"DSE/CSE walk-forward skill are consistent (DSE={dse_skill}, CSE={cse_skill}); pooling adds sample size without hiding a weak exchange"


def _final_fit_and_save_next_close(panel: pd.DataFrame, eval_result: dict, *, exchange_scope: str, model_path: Path) -> dict:
    X = panel[FEATURE_COLS].clip(lower=-50, upper=50)
    y = panel["fwd_ret_1"].clip(-0.2, 0.2).to_numpy()
    imputer = fit_median_imputer(X)
    X_i = apply_imputer(imputer, X)
    classifier, regressor = _fit_zero_inflated_next_close(X_i, y)

    skill = eval_result.get("skill_vs_naive")
    is_active = skill is not None and skill > 0
    status = STATUS_ACTIVE if is_active else STATUS_EXPERIMENTAL

    backup_path = backup_existing_model(model_path)
    version = new_version_tag()
    data_cutoff = panel["date"].max().date()
    model_metrics = eval_result.get("model_metrics", {})

    joblib.dump(
        {
            "classifier": classifier,
            "regressor": regressor,
            "imputer": imputer,
            "features": FEATURE_COLS,
            "version": version,
            "exchange_scope": exchange_scope,
            "status": status,
            "walk_forward": eval_result,
            "data_cutoff": data_cutoff.isoformat(),
            "trained_rows": int(len(panel)),
            "mae": model_metrics.get("mae"),
            "direction_hit": model_metrics.get("direction_hit_rate"),
            "skill_vs_naive": skill,
        },
        model_path,
    )

    record_model_version(
        model_name=MODEL_NAME,
        version=version,
        exchange_scope=exchange_scope,
        status=status,
        is_active=is_active,
        data_cutoff=data_cutoff,
        feature_schema=FEATURE_COLS,
        train_rows=int(len(panel)),
        fold_metadata=eval_result.get("folds", []),
        metrics={
            "model": model_metrics,
            "baselines": eval_result.get("baseline_metrics"),
            "skill_vs_baseline": eval_result.get("skill_vs_baseline"),
        },
        file_path=str(model_path),
        backup_path=backup_path,
        notes="" if is_active else "Non-positive out-of-sample skill vs zero-return baseline; not deployed for confident output.",
    )

    if exchange_scope == "combined":
        state = get_learn_state()
        state.last_trained_at = timezone.now()
        state.extras = {
            **(state.extras or {}),
            "ml_mae_return": model_metrics.get("mae"),
            "ml_direction_hit": model_metrics.get("direction_hit_rate"),
            "ml_skill_vs_naive": skill,
            "ml_train_rows": int(len(panel)),
            "feature_cols": FEATURE_COLS,
            "ml_status": status,
            "ml_version": version,
        }
        state.save(update_fields=["last_trained_at", "extras", "updated_at"])

    return {
        "ok": True,
        "status": status,
        "is_active": is_active,
        "mae_return": model_metrics.get("mae"),
        "direction_hit": model_metrics.get("direction_hit_rate"),
        "skill_vs_naive": skill,
        "train_rows": int(len(panel)),
        "test_rows": model_metrics.get("n"),
        "version": version,
        "path": str(model_path),
        "backup_path": backup_path,
    }


def train_next_close_model(limit_stocks: int = 80) -> dict:
    """Evaluate + (re)train the next-day-close regressor. Always
    evaluates DSE and CSE separately via chronological walk-forward CV;
    only pools them into one deployed model when
    _justify_combining_next_close() supports it."""
    _clear_context_cache()
    dse_panel = _build_next_close_panel(Exchange.DSE, limit_stocks=limit_stocks)
    cse_panel = _build_next_close_panel(Exchange.CSE, limit_stocks=limit_stocks)

    if dse_panel.empty and cse_panel.empty:
        return {"ok": False, "error": "Not enough data to train next-close model"}

    dse_eval = _walk_forward_evaluate_next_close(dse_panel) if len(dse_panel) >= MIN_PANEL_ROWS else {"ok": False, "error": f"insufficient DSE rows ({len(dse_panel)})"}
    cse_eval = _walk_forward_evaluate_next_close(cse_panel) if len(cse_panel) >= MIN_PANEL_ROWS else {"ok": False, "error": f"insufficient CSE rows ({len(cse_panel)})"}

    panel_all = pd.concat([dse_panel, cse_panel], ignore_index=True) if not (dse_panel.empty or cse_panel.empty) else (dse_panel if not dse_panel.empty else cse_panel)
    panel_all = panel_all.sort_values("date", kind="mergesort").reset_index(drop=True)
    combined_eval = _walk_forward_evaluate_next_close(panel_all) if len(panel_all) >= MIN_PANEL_ROWS else {"ok": False, "error": f"insufficient combined rows ({len(panel_all)})"}

    justified, reason = _justify_combining_next_close(dse_eval, cse_eval)
    evaluation = {"dse": dse_eval, "cse": cse_eval, "combined": combined_eval, "combine_justified": justified, "combine_reason": reason}

    deployed = {}
    if justified and combined_eval.get("ok"):
        deployed["combined"] = _final_fit_and_save_next_close(panel_all, combined_eval, exchange_scope="combined", model_path=MODEL_PATH)
    else:
        if dse_eval.get("ok") and len(dse_panel) >= MIN_PANEL_ROWS:
            deployed["DSE"] = _final_fit_and_save_next_close(dse_panel, dse_eval, exchange_scope=Exchange.DSE, model_path=MODEL_PATH_BY_EXCHANGE[Exchange.DSE])
        if cse_eval.get("ok") and len(cse_panel) >= MIN_PANEL_ROWS:
            deployed["CSE"] = _final_fit_and_save_next_close(cse_panel, cse_eval, exchange_scope=Exchange.CSE, model_path=MODEL_PATH_BY_EXCHANGE[Exchange.CSE])
        if not deployed:
            return {"ok": False, "error": "neither exchange had enough data to evaluate/train separately", "evaluation": evaluation}

    result = {"ok": True, "evaluation": evaluation, "deployed": deployed}
    if "combined" in deployed:
        result.update({k: v for k, v in deployed["combined"].items() if k != "ok"})
    return result


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
        df = prices_to_df(stock.prices.live().filter(date__gte=hist_start, date__lte=end))
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
