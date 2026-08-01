"""
Predictive estimates from historical analogues + scoring.

IMPORTANT: Outputs are probabilistic estimates from past 1y behaviour —
NOT guarantees. Labels like "mature in X days" mean historical average
time to a +target% move after similar setups.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from market.models import SignalAction
from market.services.indicators import compute_indicators
from market.services.patterns import detect_patterns
from market.services.price_format import round_to_tick


@dataclass
class Prediction:
    action: str
    score: float
    confidence: float
    risk_level: str
    is_safe_buy: bool
    maturity_days_est: int | None
    peak_days_est: int | None
    expected_return_pct: float | None
    probability: float | None
    rationale: str
    features: dict
    patterns: list[dict]


def _risk_from_vol(vol: float | None, group: str) -> str:
    if group == "Z":
        return "high"
    if vol is None:
        return "medium"
    if vol < 0.25:
        return "low"
    if vol > 0.45:
        return "high"
    return "medium"


def _score_from_row(row: pd.Series, patterns: list[dict], group: str) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []

    rsi = row.get("rsi_14")
    if pd.notna(rsi):
        if rsi < 35:
            score += 18
            reasons.append(f"RSI oversold ({rsi:.0f})")
        elif rsi > 65:
            score -= 18
            reasons.append(f"RSI elevated ({rsi:.0f})")
        else:
            score += 4

    if pd.notna(row.get("macd_hist")):
        if row["macd_hist"] > 0:
            score += 10
            reasons.append("MACD histogram positive")
        else:
            score -= 8
            reasons.append("MACD histogram negative")

    if pd.notna(row.get("sma_20")) and pd.notna(row.get("sma_50")):
        if row["close"] > row["sma_20"] > row["sma_50"]:
            score += 15
            reasons.append("Short-term uptrend stack")
        elif row["close"] < row["sma_20"] < row["sma_50"]:
            score -= 15
            reasons.append("Short-term downtrend stack")

    if pd.notna(row.get("return_20d")):
        r20 = float(row["return_20d"]) * 100
        if 0 < r20 < 12:
            score += 8
            reasons.append(f"Healthy 20d momentum ({r20:.1f}%)")
        elif r20 > 25:
            score -= 6
            reasons.append("Extended 20d rally — chase risk")
        elif r20 < -15:
            score += 5
            reasons.append("Deep 20d pullback — mean-reversion watch")

    if pd.notna(row.get("volume_sma_20")) and row["volume_sma_20"] > 0:
        vr = float(row["volume"]) / float(row["volume_sma_20"])
        if vr > 1.5 and row.get("return_5d", 0) > 0:
            score += 8
            reasons.append("Rising price on above-average volume")

    for p in patterns:
        delta = 12 * p["strength"] * (1 if p["direction"] == "bullish" else -1 if p["direction"] == "bearish" else 0)
        score += delta
        if abs(delta) >= 6:
            reasons.append(p["name"])

    if group == "Z":
        score -= 25
        reasons.append("Z-group risk penalty")
    elif group == "A":
        score += 5

    return float(np.clip(score, -100, 100)), reasons


MIN_SAMPLES_FOR_RANGE = 10  # below this, a percentile range/downside figure is not statistically supportable


def estimate_maturity_and_peak(df: pd.DataFrame, target_pct: float = 0.05) -> dict:
    """
    Walk history: after bullish-ish conditions (RSI<40 or MACD cross up),
    measure days until +target_pct and days to local peak within 40 sessions.

    avg_return is the mean of each episode's *best* return within the
    window (peak_ret) — an inherently optimistic, upside-only figure. To
    avoid presenting that as if it were a single confident forecast,
    also track each episode's *worst* return within the same window
    (trough_ret) as a downside scenario, plus the 25th/75th percentile of
    peak returns as a range — both only reported once there are enough
    samples to be statistically meaningful (MIN_SAMPLES_FOR_RANGE).
    """
    data = compute_indicators(df)
    if len(data) < 80:
        return {
            "maturity_days": None,
            "peak_days": None,
            "hit_rate": None,
            "avg_return": None,
            "samples": 0,
            "return_p25": None,
            "return_p75": None,
            "downside_return": None,
        }

    maturities, peaks, returns, troughs, hits = [], [], [], [], 0
    for i in range(40, len(data) - 40):
        row = data.iloc[i]
        rsi = row.get("rsi_14")
        macd_up = (
            pd.notna(row.get("macd"))
            and pd.notna(row.get("macd_signal"))
            and data.iloc[i - 1]["macd"] <= data.iloc[i - 1]["macd_signal"]
            and row["macd"] > row["macd_signal"]
        )
        setup = (pd.notna(rsi) and rsi < 40) or macd_up
        if not setup:
            continue
        entry = float(row["close"])
        if entry <= 0:
            continue
        future = data.iloc[i + 1 : i + 41]
        if future.empty:
            continue
        # Days to target
        matured = None
        for j, (_, frow) in enumerate(future.iterrows(), start=1):
            if frow["close"] >= entry * (1 + target_pct):
                matured = j
                break
        peak_idx = int(future["close"].values.argmax())
        peak_days = peak_idx + 1
        peak_ret = (float(future["close"].iloc[peak_idx]) / entry - 1) * 100
        trough_ret = (float(future["close"].min()) / entry - 1) * 100
        if matured:
            maturities.append(matured)
            hits += 1
        peaks.append(peak_days)
        returns.append(peak_ret)
        troughs.append(trough_ret)

    samples = len(returns)
    if samples == 0:
        return {
            "maturity_days": None,
            "peak_days": None,
            "hit_rate": None,
            "avg_return": None,
            "samples": 0,
            "return_p25": None,
            "return_p75": None,
            "downside_return": None,
        }

    has_range = samples >= MIN_SAMPLES_FOR_RANGE
    return {
        "maturity_days": int(round(float(np.median(maturities)))) if maturities else None,
        "peak_days": int(round(float(np.median(peaks)))),
        "hit_rate": hits / samples,
        "avg_return": float(np.mean(returns)),
        "samples": samples,
        "return_p25": float(np.percentile(returns, 25)) if has_range else None,
        "return_p75": float(np.percentile(returns, 75)) if has_range else None,
        # Mean worst-point-in-window return across episodes — a typical
        # downside scenario, not a worst-case guarantee.
        "downside_return": float(np.mean(troughs)) if has_range else None,
    }


def action_from_score(score: float) -> str:
    if score >= 25:
        return SignalAction.BUY
    if score <= -25:
        return SignalAction.SELL
    if abs(score) >= 12:
        return SignalAction.WATCH
    return SignalAction.HOLD


RESEARCH_DISCLAIMER = (
    "Experimental research output, not investment advice. Scores, forecasts, and "
    "“research candidate” flags are probabilistic estimates from historical "
    "analogues over a limited lookback window — they are not guarantees, "
    "recommendations, or verified trading signals. The underlying model is under "
    "active development and its accuracy is not independently verified."
)

CONFIDENCE_SCALE = [
    {"key": "very_high", "label": "Very high", "min": 0.80, "max": 1.0, "hint": "Strong sample support; near-term horizon"},
    {"key": "high", "label": "High", "min": 0.60, "max": 0.80, "hint": "Solid analogues; moderate uncertainty"},
    {"key": "moderate", "label": "Moderate", "min": 0.40, "max": 0.60, "hint": "Useful directional hint only"},
    {"key": "low", "label": "Low", "min": 0.20, "max": 0.40, "hint": "Wide outcome range; treat cautiously"},
    {"key": "very_low", "label": "Very low", "min": 0.0, "max": 0.20, "hint": "Speculative; far horizon or thin history"},
]


def confidence_band(confidence: float) -> dict:
    c = float(np.clip(confidence, 0.0, 1.0))
    for band in CONFIDENCE_SCALE:
        if c >= band["min"]:
            return {**band, "confidence": round(c, 3)}
    return {**CONFIDENCE_SCALE[-1], "confidence": round(c, 3)}


def _trading_days_between(start: pd.Timestamp, end: pd.Timestamp) -> int:
    """Approximate BD trading days (Sun–Thu) between two calendar dates."""
    if end <= start:
        return 0
    days = pd.bdate_range(start=start + pd.Timedelta(days=1), end=end, freq="C", weekmask="Sun Mon Tue Wed Thu")
    return int(len(days))


def predict_price_at_date(df: pd.DataFrame, target_date) -> dict:
    """
    Estimate close price on a chosen date using historical forward-return analogues.

    Past dates with known bars return the actual close (very high confidence).
    Future dates use the median N-session forward return from history, with a
    percentile band and confidence that decays with horizon.
    """
    empty = {
        "ok": False,
        "error": "Insufficient price history",
        "confidence_scale": CONFIDENCE_SCALE,
    }
    if df is None or df.empty or len(df) < 30:
        return empty

    data = df.copy()
    data["date"] = pd.to_datetime(data["date"]).dt.normalize()
    data = data.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    target = pd.Timestamp(target_date).normalize()
    last = data.iloc[-1]
    last_date = pd.Timestamp(last["date"]).normalize()
    last_close = float(last["close"])

    # Exact historical bar
    hit = data.loc[data["date"] == target]
    if not hit.empty:
        close = float(hit.iloc[0]["close"])
        conf = 0.99
        band = confidence_band(conf)
        return {
            "ok": True,
            "mode": "actual",
            "target_date": target.strftime("%Y-%m-%d"),
            "last_date": last_date.strftime("%Y-%m-%d"),
            "last_close": round(last_close, 2),
            "horizon_trading_days": 0,
            "predicted_price": round(close, 2),
            "price_low": round(close, 2),
            "price_high": round(close, 2),
            "price_downside": None,
            "downside_note": "Selected date is recorded history, not a forecast — no downside scenario applies.",
            "expected_return_pct": round((close / last_close - 1) * 100, 2) if last_close else None,
            "confidence": conf,
            "confidence_label": band["label"],
            "confidence_key": band["key"],
            "samples": 1,
            "rationale": "Selected date is in recorded history — showing actual close.",
            "confidence_scale": CONFIDENCE_SCALE,
        }

    if target < last_date:
        return {
            "ok": False,
            "error": "No price bar on that historical date (holiday or missing data).",
            "target_date": target.strftime("%Y-%m-%d"),
            "last_date": last_date.strftime("%Y-%m-%d"),
            "confidence_scale": CONFIDENCE_SCALE,
        }

    horizon = _trading_days_between(last_date, target)
    if horizon <= 0:
        # Same calendar day as last bar but not matched (shouldn't happen) or weekend
        horizon = 1
    if horizon > 180:
        return {
            "ok": False,
            "error": "Choose a date within ~6 months ahead for a meaningful estimate.",
            "target_date": target.strftime("%Y-%m-%d"),
            "last_date": last_date.strftime("%Y-%m-%d"),
            "horizon_trading_days": horizon,
            "confidence_scale": CONFIDENCE_SCALE,
        }

    closes = data["close"].astype(float).values
    # Historical N-day forward returns (need room after each index)
    fwd = []
    for i in range(0, len(closes) - horizon):
        entry = closes[i]
        if entry <= 0:
            continue
        fwd.append(closes[i + horizon] / entry - 1)

    if len(fwd) < 8:
        # Fall back to drift from recent daily returns scaled by horizon
        rets = pd.Series(closes).pct_change().dropna()
        if len(rets) < 10:
            return empty
        mu = float(rets.tail(60).mean())
        sigma = float(rets.tail(60).std() or 0.02)
        med = (1 + mu) ** horizon - 1
        p25 = med - 0.675 * sigma * np.sqrt(horizon)
        p75 = med + 0.675 * sigma * np.sqrt(horizon)
        # 10th percentile (one-sided ~90%) normal approx — the downside scenario
        p10 = med - 1.2816 * sigma * np.sqrt(horizon)
        samples = len(rets.tail(60))
        method = "drift"
    else:
        arr = np.array(fwd)
        med = float(np.median(arr))
        p25 = float(np.percentile(arr, 25))
        p75 = float(np.percentile(arr, 75))
        p10 = float(np.percentile(arr, 10))
        samples = len(arr)
        method = "analogues"

    has_downside = samples >= MIN_SAMPLES_FOR_RANGE
    downside_gap = max(0.0, p25 - p10)  # distance from p25 down to p10, preserved through any re-centering below

    learn_note = ""
    # Next-day close: apply learned bias / ML blend from the after-close learn loop
    if horizon == 1:
        try:
            from market.services.close_learn import forecast_next_close, get_learn_state

            bias = float(get_learn_state().return_bias or 0.0)
            nd = forecast_next_close(data, return_bias=bias)
            if nd:
                med = float(nd["predicted_return"])
                # Keep band centered on corrected mid
                half = abs(p75 - p25) / 2 if p75 != p25 else 0.01
                p25 = med - half
                p75 = med + half
                method = nd.get("method", method)
                learn_note = f" Close-learn bias applied ({bias:+.4f})."
        except Exception:
            pass

    predicted = last_close * (1 + med)
    low = last_close * (1 + p25)
    high = last_close * (1 + p75)
    downside_price = last_close * (1 + p25 - downside_gap) if has_downside else None

    # Confidence: more samples & shorter horizon → higher; high dispersion → lower
    sample_factor = min(1.0, samples / 40)
    horizon_factor = float(np.exp(-horizon / 55))
    spread = abs(p75 - p25)
    spread_penalty = float(np.clip(1.0 - spread * 1.2, 0.35, 1.0))
    confidence = float(np.clip(0.22 + 0.55 * sample_factor * horizon_factor * spread_penalty, 0.08, 0.88))
    if method == "drift":
        confidence = min(confidence, 0.45)

    band = confidence_band(confidence)
    rationale = (
        f"Based on {samples} historical {horizon}-session forward moves "
        f"({'median analogue return' if 'analogue' in method else 'recent drift projection'}). "
        f"Confidence falls as the target date moves further out."
        f"{learn_note}"
    )

    return {
        "ok": True,
        "mode": "forecast",
        "target_date": target.strftime("%Y-%m-%d"),
        "last_date": last_date.strftime("%Y-%m-%d"),
        "last_close": round(last_close, 2),
        "horizon_trading_days": horizon,
        "predicted_price": round_to_tick(predicted),
        "price_low": round_to_tick(min(low, high)),
        "price_high": round_to_tick(max(low, high)),
        "price_downside": round_to_tick(downside_price),
        "downside_note": (
            "10th-percentile downside scenario from historical analogues — a typical bad outcome, not a worst case."
            if has_downside
            else "Not enough historical analogues for a statistically supportable downside scenario."
        ),
        "expected_return_pct": round(med * 100, 2),
        "confidence": round(confidence, 3),
        "confidence_label": band["label"],
        "confidence_key": band["key"],
        "samples": samples,
        "method": method,
        "rationale": rationale,
        "confidence_scale": CONFIDENCE_SCALE,
    }


def predict_stock(df: pd.DataFrame, group: str = "A", pe_ratio: float | None = None) -> Prediction:
    if df is None or len(df) < 30:
        return Prediction(
            action=SignalAction.HOLD,
            score=0,
            confidence=0.1,
            risk_level="high",
            is_safe_buy=False,
            maturity_days_est=None,
            peak_days_est=None,
            expected_return_pct=None,
            probability=None,
            rationale="Insufficient history (need ~30+ sessions).",
            features={},
            patterns=[],
        )

    data = compute_indicators(df)
    patterns = detect_patterns(df)
    row = data.iloc[-1]
    score, reasons = _score_from_row(row, patterns, group)

    # Soft PE adjustment
    if pe_ratio is not None and pe_ratio > 0:
        if pe_ratio < 12:
            score += 6
            reasons.append(f"Attractive P/E ({pe_ratio:.1f})")
        elif pe_ratio > 30:
            score -= 8
            reasons.append(f"Elevated P/E ({pe_ratio:.1f})")

    score = float(np.clip(score, -100, 100))
    action = action_from_score(score)
    est = estimate_maturity_and_peak(df)
    vol = None if pd.isna(row.get("volatility_20")) else float(row["volatility_20"])
    risk = _risk_from_vol(vol, group)

    confidence = min(0.92, 0.35 + abs(score) / 150 + (0.15 if (est["samples"] or 0) > 10 else 0))
    if est["hit_rate"] is not None:
        confidence = min(0.95, (confidence + est["hit_rate"]) / 2 + 0.15)

    is_safe = (
        action == SignalAction.BUY
        and risk != "high"
        and group != "Z"
        and score >= 28
        and confidence >= 0.4
        and (est["hit_rate"] is None or est["hit_rate"] >= 0.35)
    )

    rationale_parts = reasons[:6]
    if est["maturity_days"]:
        rationale_parts.append(
            f"Historically, similar setups matured (~+5%) in ~{est['maturity_days']} days "
            f"(hit rate {est['hit_rate']:.0%} over {est['samples']} samples)."
        )
    if est["peak_days"]:
        rationale_parts.append(f"Median days to local peak after setup: ~{est['peak_days']}.")
    if est["return_p25"] is not None and est["return_p75"] is not None:
        rationale_parts.append(
            f"Peak-return range across those episodes: {est['return_p25']:.1f}% to {est['return_p75']:.1f}% "
            f"(typical downside within the same window: {est['downside_return']:.1f}%)."
        )

    features = {
        "rsi_14": None if pd.isna(row.get("rsi_14")) else float(row["rsi_14"]),
        "macd_hist": None if pd.isna(row.get("macd_hist")) else float(row["macd_hist"]),
        "sma_20": None if pd.isna(row.get("sma_20")) else float(row["sma_20"]),
        "sma_50": None if pd.isna(row.get("sma_50")) else float(row["sma_50"]),
        "close": float(row["close"]),
        "volatility_20": vol,
        "return_20d": None if pd.isna(row.get("return_20d")) else float(row["return_20d"]),
        "pe_ratio": pe_ratio,
        "history_samples": est["samples"],
        "hit_rate": est["hit_rate"],
        "peak_return_p25": est["return_p25"],
        "peak_return_p75": est["return_p75"],
        "downside_return_pct": est["downside_return"],
    }

    return Prediction(
        action=action,
        score=round(score, 2),
        confidence=round(confidence, 3),
        risk_level=risk,
        is_safe_buy=is_safe,
        maturity_days_est=est["maturity_days"],
        peak_days_est=est["peak_days"],
        expected_return_pct=None if est["avg_return"] is None else round(est["avg_return"], 2),
        probability=None if est["hit_rate"] is None else round(est["hit_rate"], 3),
        rationale="; ".join(rationale_parts) if rationale_parts else "Neutral posture.",
        features=features,
        patterns=patterns,
    )
