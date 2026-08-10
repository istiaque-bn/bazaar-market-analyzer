"""
ML Reliability Monitor — prediction capture.

Writes immutable market.models.PredictionSnapshot rows from each model
family's existing daily prediction artifact rather than by re-running
inference:
  - forward_return_rf: from that day's AnalysisResult rows where ml_score
    is set (i.e. the classifier actually contributed to the blended score).
    AnalysisResult.ml_score is round(ml_prob * 100, 2) — see
    market.services.analyzer.analyze_stock — so the raw probability is
    recovered as ml_score / 100.
  - next_close_rf: from that day's NextDayCloseForecast rows (written by
    market.services.close_learn.generate_forecasts_for_as_of).

Capture is idempotent: PredictionSnapshot's unique constraint on
(model_family, model_version_tag, stock_trading_code, exchange,
data_cutoff_date, horizon_trading_days) means re-running for a day that's
already captured is a no-op (get_or_create just returns the existing row).
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date, timedelta

from django.utils import timezone

from market.models import (
    AnalysisResult,
    MLModelVersion,
    NextDayCloseForecast,
    PredictionSnapshot,
    Stock,
)
from market.services.close_learn import FEATURE_COLS as NEXT_CLOSE_FEATURE_COLS
from market.services.close_learn import MODEL_NAME as NEXT_CLOSE_MODEL_NAME
from market.services.indicators import prices_to_df
from market.services.market_hours import TRADING_WEEKDAYS
from market.services.ml_model import FORWARD_HORIZON_TRADING_DAYS
from market.services.ml_model import MODEL_NAME as FORWARD_RETURN_MODEL_NAME
from market.services.ml_training import active_model_version
from market.services.predictor import confidence_band, predict_stock
from market.services.signal_status import data_freshness, liquidity_note
from market.services.trading_calendar import closure_reason

logger = logging.getLogger(__name__)


def _feature_schema_version(feature_schema: list[str] | None) -> str:
    cols = sorted(feature_schema or [])
    return hashlib.sha256(",".join(cols).encode()).hexdigest()[:16]


def nth_trading_day_after(from_date: date, n: int, max_calendar_days: int = 400) -> date:
    """The n-th trading session strictly after from_date, skipping
    weekends and named holidays via trading_calendar.closure_reason —
    calendar-day arithmetic is never used for a trading-session date.
    Bounded loop so a corrupt/huge n can't hang; falls back to a rough
    calendar estimate if the bound is hit (should not happen for the
    horizons this project actually uses, <= 10)."""
    d = from_date
    found = 0
    steps = 0
    while found < n and steps < max_calendar_days:
        d = d + timedelta(days=1)
        steps += 1
        if d.weekday() in TRADING_WEEKDAYS and closure_reason(d) is None:
            found += 1
    if found < n:
        return from_date + timedelta(days=n * 2)
    return d


def _resolve_active_version(model_name: str, exchange: str) -> MLModelVersion | None:
    """Same combined-then-per-exchange precedence market.services.ml_model
    and close_learn use at inference time (see their load_model()/
    load_next_close_model()) — best-effort resolution of which version
    most likely produced this prediction; capture runs same-day right
    after analysis, so drift between the two lookups is negligible."""
    combined = active_model_version(model_name, exchange_scope="combined")
    if combined is not None:
        return combined
    return active_model_version(model_name, exchange_scope=exchange)


def _naive_majority_class(version: MLModelVersion | None) -> bool | None:
    """Majority class observed in the most recent walk-forward fold's
    training data — the same majority-class baseline concept the
    deployment gate itself is scored against (see
    ml_training.majority_class_baseline)."""
    if version is None or not version.fold_metadata:
        return None
    last_fold = version.fold_metadata[-1]
    balance = last_fold.get("train_class_balance") or {}
    if not balance:
        return None
    majority_key = max(balance, key=lambda k: balance[k])
    return majority_key == "1"


def _data_quality(stock: Stock, analysis: AnalysisResult | None) -> tuple[str, dict]:
    fresh = data_freshness(analysis, stock)
    liq = liquidity_note(stock)
    if fresh.get("is_stale"):
        return "stale", {"freshness": fresh, "liquidity": liq}
    if liq.get("label") == "thin":
        return "thin_liquidity", {"freshness": fresh, "liquidity": liq}
    return "ok", {"freshness": fresh, "liquidity": liq}


def _persistence_return(stock: Stock, as_of: date) -> float | None:
    """Prior session's 1-day return as of `as_of` — the same 'persistence'
    baseline concept ml_training.persistence_baseline formalizes for
    walk-forward evaluation, read directly off the last two bars here."""
    bars = list(stock.prices.live().filter(date__lte=as_of).order_by("-date").values_list("close", flat=True)[:2])
    if len(bars) < 2 or not bars[1]:
        return None
    return float(bars[0]) / float(bars[1]) - 1.0


def capture_forward_return_snapshots(as_of: date | None = None) -> dict:
    as_of = as_of or timezone.localdate()
    qs = AnalysisResult.objects.filter(as_of=as_of, ml_score__isnull=False).select_related("stock")
    created = skipped = 0
    for analysis in qs.iterator():
        stock = analysis.stock
        if stock is None:
            skipped += 1
            continue
        version = _resolve_active_version(FORWARD_RETURN_MODEL_NAME, stock.exchange)
        if version is None:
            skipped += 1
            continue
        reference_close = (analysis.features or {}).get("close")
        if reference_close is None:
            skipped += 1
            continue

        predicted_probability = float(analysis.ml_score) / 100.0
        predicted_class = predicted_probability > 0.5

        df = prices_to_df(stock.prices.live().filter(date__lte=as_of))
        rule_pred = predict_stock(df, group=stock.group, pe_ratio=stock.pe_ratio)
        rule_baseline_class = rule_pred.score > 0

        target_date = nth_trading_day_after(as_of, FORWARD_HORIZON_TRADING_DAYS)
        confidence_value = abs(predicted_probability - 0.5) * 2
        dq_status, dq_notes = _data_quality(stock, analysis)

        _, was_created = PredictionSnapshot.objects.get_or_create(
            model_family=PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF,
            model_version_tag=version.version,
            stock_trading_code=stock.trading_code,
            exchange=stock.exchange,
            data_cutoff_date=as_of,
            horizon_trading_days=FORWARD_HORIZON_TRADING_DAYS,
            defaults={
                "model_version": version,
                "feature_schema_version": _feature_schema_version(version.feature_schema),
                "stock": stock,
                "target_date": target_date,
                "reference_close": float(reference_close),
                "predicted_class": predicted_class,
                "predicted_probability": predicted_probability,
                "rule_baseline_class": rule_baseline_class,
                "naive_baseline_class": _naive_majority_class(version),
                "confidence_value": confidence_value,
                "confidence_label": confidence_band(confidence_value)["label"],
                "data_quality_status": dq_status,
                "data_quality_notes": dq_notes,
            },
        )
        if was_created:
            created += 1
        else:
            skipped += 1
    return {"ok": True, "as_of": as_of.isoformat(), "created": created, "already_captured": skipped}


def capture_next_close_snapshots(as_of: date | None = None) -> dict:
    as_of = as_of or timezone.localdate()
    qs = NextDayCloseForecast.objects.filter(as_of=as_of).select_related("stock")
    created = skipped = 0
    for fc in qs.iterator():
        stock = fc.stock
        if stock is None:
            skipped += 1
            continue
        version = _resolve_active_version(NEXT_CLOSE_MODEL_NAME, stock.exchange)
        # The analogue/bias learner may legitimately be served while its ML
        # challenger is experimental. Capture it too, using a stable baseline
        # tag, so a missing active model can never erase point-in-time evidence.
        version_tag = version.version if version is not None else "next-close-baseline-v1"

        analysis = AnalysisResult.objects.filter(stock=stock, as_of=as_of).first()
        dq_status, dq_notes = _data_quality(stock, analysis)

        _, was_created = PredictionSnapshot.objects.get_or_create(
            model_family=PredictionSnapshot.ModelFamily.NEXT_CLOSE_RF,
            model_version_tag=version_tag,
            stock_trading_code=stock.trading_code,
            exchange=stock.exchange,
            data_cutoff_date=as_of,
            horizon_trading_days=1,
            defaults={
                "model_version": version,
                "feature_schema_version": _feature_schema_version(version.feature_schema if version else NEXT_CLOSE_FEATURE_COLS),
                "stock": stock,
                "target_date": fc.target_date,
                "reference_close": float(fc.last_close),
                "predicted_return": fc.predicted_return,
                "predicted_price": fc.predicted_close,
                "rule_baseline_return": _persistence_return(stock, as_of),
                "naive_baseline_return": 0.0,
                "confidence_value": fc.confidence,
                "confidence_label": confidence_band(fc.confidence)["label"],
                "data_quality_status": dq_status,
                "data_quality_notes": dq_notes,
            },
        )
        if was_created:
            created += 1
        else:
            skipped += 1
    return {"ok": True, "as_of": as_of.isoformat(), "created": created, "already_captured": skipped}


def capture_predictions(as_of: date | None = None) -> dict:
    as_of = as_of or timezone.localdate()
    forward = capture_forward_return_snapshots(as_of)
    next_close = capture_next_close_snapshots(as_of)
    return {"ok": True, "as_of": as_of.isoformat(), "forward_return_rf": forward, "next_close_rf": next_close}
