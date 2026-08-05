"""
ML Reliability Monitor — orchestrator.

Single source of truth reused by the management command
(assess_ml_reliability), the Celery task (market.tasks.assess_ml_reliability)
and the staff dashboard/API, so all three present exactly the same
assessment logic and thresholds.

Pipeline per run: capture today's predictions -> settle due predictions
-> (unless settle_only) compute + persist one ReliabilityAssessment per
(model family, exchange, horizon, window). dry_run makes the whole run
read-only: no capture/settlement writes, no ReliabilityAssessment rows —
just a preview of what the metrics currently look like.
"""
from __future__ import annotations

from datetime import date

from django.utils import timezone

from market.models import Exchange, PredictionSnapshot, ReliabilityAssessment
from market.services.ml_model import FORWARD_HORIZON_TRADING_DAYS
from market.services.ml_training import active_model_version
from market.services.reliability_capture import capture_predictions
from market.services.reliability_drift import PSI_MAJOR, PSI_MODERATE, assess_drift
from market.services.reliability_metrics import (
    HIGH_CALIBRATION_ERROR,
    MIN_SAMPLES_INSUFFICIENT,
    MIN_SAMPLES_WATCH,
    WINDOW_SIZES,
    bootstrap_ci,
    compute_classifier_metrics,
    compute_economic_diagnostics,
    compute_regressor_metrics,
    determine_status,
)
from market.services.reliability_recommend import cross_exchange_divergence_recommendation, generate_recommendations
from market.services.reliability_settlement import settle_predictions

FAMILY_HORIZONS = {
    PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF: FORWARD_HORIZON_TRADING_DAYS,
    PredictionSnapshot.ModelFamily.NEXT_CLOSE_RF: 1,
}


def _resolve_active_version(model_name: str, exchange: str):
    combined = active_model_version(model_name, exchange_scope="combined")
    if combined is not None:
        return combined
    return active_model_version(model_name, exchange_scope=exchange)


def _resolve_version_for_assessment(model_name: str, exchange: str, version_tag: str | None):
    """When --version is given, evaluate that exact (possibly inactive/
    historical) version instead of whatever is currently active — lets a
    staff member inspect a specific past version's track record."""
    if version_tag:
        from market.models import MLModelVersion

        return MLModelVersion.objects.filter(model_name=model_name, version=version_tag).first()
    return _resolve_active_version(model_name, exchange)


def _row_dict(snap: PredictionSnapshot) -> dict:
    return {
        "data_cutoff_date": snap.data_cutoff_date,
        "target_date": snap.target_date,
        "predicted_class": snap.predicted_class,
        "predicted_probability": snap.predicted_probability,
        "predicted_return": snap.predicted_return,
        "predicted_price": snap.predicted_price,
        "rule_baseline_class": snap.rule_baseline_class,
        "rule_baseline_return": snap.rule_baseline_return,
        "naive_baseline_class": snap.naive_baseline_class,
        "naive_baseline_return": snap.naive_baseline_return,
        "outcome_class": snap.outcome_class,
        "outcome_return": snap.outcome_return,
        "outcome_price": snap.outcome_price,
    }


def _threshold_config() -> dict:
    return {
        "min_samples_insufficient": MIN_SAMPLES_INSUFFICIENT,
        "min_samples_watch": MIN_SAMPLES_WATCH,
        "high_calibration_error": HIGH_CALIBRATION_ERROR,
        "psi_moderate": PSI_MODERATE,
        "psi_major": PSI_MAJOR,
    }


def _assess_one(
    *, family: str, exchange: str, horizon: int, window_size: int, version, dry_run: bool
) -> tuple[dict, ReliabilityAssessment | None]:
    window_label = str(window_size)

    # Scoped to exactly this model version's own settled snapshots — a
    # retrain must not inherit (or be blamed for) a prior version's track
    # record; each version's reliability window starts fresh from its own
    # first captured prediction. See test_reliability_report.py's
    # ModelVersionIsolationTests.
    qs = PredictionSnapshot.objects.filter(
        model_family=family,
        exchange=exchange,
        horizon_trading_days=horizon,
        model_version_tag=version.version if version else "__no_active_version__",
        settlement_status=PredictionSnapshot.SettlementStatus.SETTLED,
    ).order_by("-data_cutoff_date")[:window_size]
    rows = [_row_dict(s) for s in qs]
    sample_count = len(rows)
    period_start = min((r["data_cutoff_date"] for r in rows), default=None)
    period_end = max((r["data_cutoff_date"] for r in rows), default=None)

    is_classifier = family == PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF
    metrics = compute_classifier_metrics(rows) if is_classifier else compute_regressor_metrics(rows)
    economic = compute_economic_diagnostics(rows, family) if sample_count else {"n": 0}
    drift = assess_drift(rows, family, model_version=version) if sample_count else {"flags": []}

    skill_key = "naive_majority_class" if is_classifier else "naive_zero_return"
    skill_vs_naive = (metrics.get("skill_vs_baseline") or {}).get(skill_key)
    calibration_error = (metrics.get("model") or {}).get("calibration_error") if is_classifier else None

    status, reasons = determine_status(sample_count, skill_vs_naive, calibration_error, drift.get("flags", []))

    ci = {}
    if sample_count >= 2:
        if is_classifier:
            correct = [1.0 if r["predicted_class"] == r["outcome_class"] else 0.0 for r in rows]
            ci["accuracy"] = bootstrap_ci(correct)
        else:
            errors = [abs((r["predicted_return"] or 0) - (r["outcome_return"] or 0)) for r in rows]
            ci["mae"] = bootstrap_ci(errors)

    recommendations = generate_recommendations(
        status=status,
        family=family,
        exchange=exchange,
        sample_count=sample_count,
        skill_vs_naive=skill_vs_naive,
        calibration_error=calibration_error,
        drift=drift,
    )

    payload = dict(
        model_family=family,
        model_version=version,
        model_version_tag=version.version if version else "",
        exchange=exchange,
        horizon_trading_days=horizon,
        window_label=window_label,
        window_size=window_size,
        period_start=period_start,
        period_end=period_end,
        sample_count=sample_count,
        status=status,
        reasons=reasons,
        recommendations=recommendations,
        metrics={("classification" if is_classifier else "regression"): metrics, "economic": economic},
        confidence_intervals=ci,
        drift=drift,
        threshold_config=_threshold_config(),
    )

    if dry_run:
        return payload, None
    obj = ReliabilityAssessment.objects.create(**payload)
    return payload, obj


def run_reliability_assessment(
    *,
    families: list[str] | None = None,
    exchanges: list[str] | None = None,
    windows: list[int] | None = None,
    version_tag: str | None = None,
    settle_only: bool = False,
    dry_run: bool = False,
    as_of: date | None = None,
) -> dict:
    from market.services.exchange_config import enabled_exchanges

    as_of = as_of or timezone.localdate()
    families = families or list(FAMILY_HORIZONS.keys())
    # A disabled exchange is never assessed by default (no new predictions
    # were captured for it — see analyzer.run_full_analysis/close_learn's
    # own exchange gating — so there'd be nothing new to assess anyway).
    # An explicit `exchanges=` argument (from the management command's
    # --exchange, a staff-triggered diagnostic) still overrides this,
    # since assessing existing historical predictions for a disabled
    # exchange is a read-only diagnostic, not new market-data processing.
    exchanges = exchanges or enabled_exchanges()
    windows = windows or WINDOW_SIZES

    result: dict = {"ok": True, "as_of": as_of.isoformat(), "dry_run": dry_run}

    if not dry_run:
        result["capture"] = capture_predictions(as_of=as_of)
        result["settlement"] = settle_predictions(through_date=as_of)
    else:
        result["capture"] = {"skipped": "dry_run"}
        result["settlement"] = {"skipped": "dry_run"}

    if settle_only:
        return result

    payloads = []
    skill_map: dict[tuple[str, int, str], dict[str, float | None]] = {}
    for family in families:
        horizon = FAMILY_HORIZONS[family]
        for exchange in exchanges:
            version = _resolve_version_for_assessment(family, exchange, version_tag)
            for window_size in windows:
                payload, _obj = _assess_one(
                    family=family, exchange=exchange, horizon=horizon, window_size=window_size, version=version, dry_run=dry_run
                )
                payloads.append(payload)
                is_classifier = family == PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF
                skill_key = "naive_majority_class" if is_classifier else "naive_zero_return"
                metric_key = "classification" if is_classifier else "regression"
                skill_val = ((payload["metrics"].get(metric_key) or {}).get("skill_vs_baseline") or {}).get(skill_key)
                skill_map.setdefault((family, horizon, str(window_size)), {})[exchange] = skill_val

    cross_flags = []
    for (family, horizon, window_label), by_ex in skill_map.items():
        rec = cross_exchange_divergence_recommendation(family, horizon, window_label, by_ex.get(Exchange.DSE), by_ex.get(Exchange.CSE))
        if rec:
            cross_flags.append({"model_family": family, "horizon_trading_days": horizon, "window_label": window_label, **rec})

    result["assessments"] = [
        {
            "model_family": p["model_family"],
            "model_version_tag": p["model_version_tag"],
            "exchange": p["exchange"],
            "horizon_trading_days": p["horizon_trading_days"],
            "window_label": p["window_label"],
            "sample_count": p["sample_count"],
            "status": p["status"],
            "reasons": p["reasons"],
            "recommendations": p["recommendations"],
            "metrics": p["metrics"],
            "confidence_intervals": p["confidence_intervals"],
            "drift": p["drift"],
        }
        for p in payloads
    ]
    result["cross_exchange_flags"] = cross_flags
    return result


def latest_assessments() -> list[ReliabilityAssessment]:
    """One row per (family, exchange, horizon, window) — the most recent
    run for each group. O(all ReliabilityAssessment rows); fine at this
    table's realistic size (a handful of groups x runs/day), not intended
    for indefinite-scale use."""
    seen = set()
    out = []
    for a in ReliabilityAssessment.objects.order_by("-run_at").iterator():
        key = (a.model_family, a.exchange, a.horizon_trading_days, a.window_label)
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


def assessment_history(model_family: str, exchange: str, horizon: int, window_label: str, limit: int = 20) -> list[ReliabilityAssessment]:
    return list(
        ReliabilityAssessment.objects.filter(
            model_family=model_family, exchange=exchange, horizon_trading_days=horizon, window_label=window_label
        ).order_by("-run_at")[:limit]
    )
