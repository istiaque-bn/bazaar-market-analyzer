"""
ML Reliability Monitor — recommendations.

Every recommendation cites the concrete metric(s) that produced it —
never a bare verdict. Automatic retraining/redeployment is never
triggered here: recommending "retrain" only ever produces a suggestion
for a human to act on via `manage.py train_ml_models`, and any resulting
candidate must still pass the existing walk-forward evaluation +
deployment gate (market.services.ml_training.record_model_version /
market.services.ml_model._final_fit_and_save) before it can ever become
active — this module has no ability to flip is_active itself.
"""
from __future__ import annotations

from market.models import ReliabilityAssessment
from market.services.reliability_metrics import HIGH_CALIBRATION_ERROR

DSE_CSE_DIVERGENCE_THRESHOLD = 0.15  # mirrors ml_model._justify_combining's own threshold


def _rec(action: str, reason: str, citing: list[str]) -> dict:
    return {"action": action, "reason": reason, "citing": citing}


def generate_recommendations(
    *,
    status: str,
    family: str,
    exchange: str,
    sample_count: int,
    skill_vs_naive: float | None,
    calibration_error: float | None,
    drift: dict,
) -> list[dict]:
    recs: list[dict] = []

    if status == ReliabilityAssessment.Status.INSUFFICIENT_DATA:
        recs.append(
            _rec(
                "collect_more_outcomes",
                f"Only {sample_count} settled predictions — cannot support any reliability claim yet.",
                [f"sample_count={sample_count}"],
            )
        )
        return recs

    if calibration_error is not None and calibration_error > HIGH_CALIBRATION_ERROR and family == "forward_return_rf":
        recs.append(
            _rec(
                "recalibrate_probabilities",
                f"Expected calibration error is {calibration_error:.3f} (> {HIGH_CALIBRATION_ERROR}) — predicted "
                "probabilities don't track observed frequencies; consider Platt scaling / isotonic recalibration "
                "before trusting confidence-based decisions.",
                [f"calibration_error={calibration_error:.3f}"],
            )
        )

    perf_drift = drift.get("performance_drift") or {}
    ref_hit, cur_hit = perf_drift.get("reference_hit_rate"), perf_drift.get("current_hit_rate")
    if ref_hit is not None and cur_hit is not None and (ref_hit - cur_hit) > 0.15:
        recs.append(
            _rec(
                "retrain_using_newer_data",
                f"Direction-hit rate degraded from {ref_hit:.3f} (older half of window) to {cur_hit:.3f} (newer "
                "half) — performance is drifting away from what the model was trained on.",
                [f"reference_hit_rate={ref_hit:.3f}", f"current_hit_rate={cur_hit:.3f}"],
            )
        )

    schema = drift.get("feature_schema") or {}
    if schema.get("changed"):
        recs.append(
            _rec(
                "inspect_specific_feature",
                "The feature columns currently used for inference differ from what this model version was trained "
                f"on — missing={schema.get('missing_features')}, newly_introduced={schema.get('newly_introduced_features')}.",
                [f"missing_features={schema.get('missing_features')}", f"newly_introduced_features={schema.get('newly_introduced_features')}"],
            )
        )

    pred_dist = drift.get("prediction_distribution") or {}
    psi = pred_dist.get("psi")
    if psi is not None and psi > 0.25:
        recs.append(
            _rec(
                "inspect_specific_feature",
                f"Predicted-value distribution shifted materially between the older and newer half of the window "
                f"(PSI={psi:.3f} > 0.25) — investigate which upstream input changed regime before trusting current "
                "predictions.",
                [f"prediction_distribution_psi={psi:.3f}"],
            )
        )

    calib_drift = drift.get("calibration_drift") or {}
    ref_ece, cur_ece = calib_drift.get("reference_ece"), calib_drift.get("current_ece")
    if ref_ece is not None and cur_ece is not None and (cur_ece - ref_ece) > 0.1:
        recs.append(
            _rec(
                "reduce_confidence_or_abstain",
                f"Calibration error worsened from {ref_ece:.3f} to {cur_ece:.3f} between halves of the window — "
                "confidence-based decisions (e.g. high-confidence-only filtering) are less trustworthy right now.",
                [f"reference_ece={ref_ece:.3f}", f"current_ece={cur_ece:.3f}"],
            )
        )

    if status in (ReliabilityAssessment.Status.DEGRADED, ReliabilityAssessment.Status.CRITICAL):
        recs.append(
            _rec(
                "downgrade_to_experimental",
                f"Status is {status} for {family}[{exchange}] — consider flipping this model version's "
                "MLModelVersion.is_active to False via /admin until skill vs. naive baseline recovers.",
                [f"status={status}", f"skill_vs_naive={skill_vs_naive}"],
            )
        )
        recs.append(
            _rec(
                "compare_against_simpler_baseline",
                "Given the current status, confirm the rule-based/naive baseline isn't already outperforming this "
                "model before spending effort on a retrain.",
                [f"status={status}"],
            )
        )

    if status == ReliabilityAssessment.Status.HEALTHY and not recs:
        recs.append(
            _rec(
                "continue_serving",
                f"Skill vs. naive baseline is positive ({skill_vs_naive:.4f}) over {sample_count} settled "
                "predictions with no calibration or drift concerns detected.",
                [f"skill_vs_naive={skill_vs_naive}", f"sample_count={sample_count}"],
            )
        )

    return recs


def cross_exchange_divergence_recommendation(
    family: str, horizon: int, window_label: str, dse_skill: float | None, cse_skill: float | None
) -> dict | None:
    """Compares DSE vs. CSE skill for the same (family, horizon, window) —
    mirrors ml_model._justify_combining's own divergence threshold, since
    a combined-scope model is only appropriate when the two exchanges
    behave consistently."""
    if dse_skill is None or cse_skill is None:
        return None
    if abs(dse_skill - cse_skill) <= DSE_CSE_DIVERGENCE_THRESHOLD:
        return None
    return _rec(
        "separate_dse_cse_models",
        f"DSE and CSE skill vs. naive baseline diverge by {abs(dse_skill - cse_skill):.3f} "
        f"(DSE={dse_skill:.3f}, CSE={cse_skill:.3f}) for {family} at horizon={horizon}, window={window_label} — "
        "a combined-scope model may be hiding a weak exchange; consider exchange-specific models.",
        [f"dse_skill_vs_naive={dse_skill:.3f}", f"cse_skill_vs_naive={cse_skill:.3f}"],
    )
