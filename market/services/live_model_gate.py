"""Automatic fail-closed gate for live ML reliability evidence."""
from __future__ import annotations

from market.models import MLModelStatus, MLModelVersion, PredictionSnapshot, ReliabilityAssessment

FORWARD_GATE_WINDOW = "90"
MIN_GATE_SAMPLES = 60


def activation_eligibility(version: MLModelVersion) -> tuple[bool, list[str]]:
    """Require healthy live evidence, both baselines, and after-cost edge."""
    assessments = ReliabilityAssessment.objects.filter(
        model_version=version,
        window_label=FORWARD_GATE_WINDOW,
    ).order_by("exchange", "-run_at")
    latest_by_exchange = {}
    for assessment in assessments:
        latest_by_exchange.setdefault(assessment.exchange, assessment)
    required_exchanges = {version.exchange_scope} if version.exchange_scope != "combined" else {"DSE", "CSE"}
    reasons = []
    for exchange in sorted(required_exchanges):
        assessment = latest_by_exchange.get(exchange)
        if assessment is None:
            reasons.append(f"{exchange}: no 90-sample live reliability assessment for this version")
            continue
        if assessment.sample_count < MIN_GATE_SAMPLES or assessment.status != ReliabilityAssessment.Status.HEALTHY:
            reasons.append(f"{exchange}: live reliability is {assessment.status} at n={assessment.sample_count}")
            continue
        metric_group = "classification" if version.model_name == "forward_return_rf" else "regression"
        skill = ((assessment.metrics or {}).get(metric_group) or {}).get("skill_vs_baseline") or {}
        required_keys = (
            ("rule_based", "naive_majority_class")
            if metric_group == "classification"
            else ("rule_based_persistence", "naive_zero_return")
        )
        for key in required_keys:
            if (skill.get(key) or 0) <= 0:
                reasons.append(f"{exchange}: non-positive skill vs {key}")
        economic = ((assessment.metrics or {}).get("economic") or {})
        after_cost = (economic.get("at_1x_cost") or {}).get("net_total_return_pct")
        if after_cost is None or after_cost <= 0:
            reasons.append(f"{exchange}: after-cost return is not positive")
        if (economic.get("benchmark_relative_return_pct") or 0) <= 0:
            reasons.append(f"{exchange}: does not beat the buy-and-hold benchmark after costs")
    return not reasons, reasons


def suspend_critical_forward_models(assessments: list[dict], *, dry_run: bool) -> list[dict]:
    """Suspend a served forward classifier after adequate critical live evidence.

    The training-time gate remains necessary, but is not sufficient: a model
    can lose its edge after deployment.  This gate only suspends; it never
    promotes or reactivates a model.  A future retrain must independently
    pass the ordinary chronological deployment gate.
    """
    decisions = []
    for assessment in assessments:
        if assessment["model_family"] != PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF:
            continue
        if assessment["window_label"] != FORWARD_GATE_WINDOW or assessment["sample_count"] < MIN_GATE_SAMPLES:
            continue
        if assessment["status"] != "critical":
            continue
        version = assessment.get("model_version")
        if version is None:
            continue
        # load_model() can prefer a combined artifact and then fall back to
        # a per-exchange artifact. Suspend both serving candidates for this
        # exchange so a critical assessment cannot be bypassed by precedence.
        scopes = {"combined", assessment["exchange"]}
        targets = MLModelVersion.objects.filter(
            model_name=version.model_name, exchange_scope__in=scopes, is_active=True
        )
        for target in targets:
            detail = {
                "model": target.model_name,
                "version": target.version,
                "exchange_scope": target.exchange_scope,
                "window": FORWARD_GATE_WINDOW,
                "sample_count": assessment["sample_count"],
                "reason": "critical live reliability assessment",
            }
            if not dry_run:
                target.is_active = False
                target.status = MLModelStatus.EXPERIMENTAL
                target.notes = (target.notes + "\n" if target.notes else "") + (
                    f"Auto-suspended: critical {FORWARD_GATE_WINDOW}-day live reliability assessment "
                    f"over {assessment['sample_count']} settled predictions."
                )
                target.save(update_fields=["is_active", "status", "notes"])
            decisions.append({**detail, "action": "would_suspend" if dry_run else "suspended"})
    return decisions
