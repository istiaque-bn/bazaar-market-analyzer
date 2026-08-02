"""
ML Reliability Monitor — drift detection.

Reference period = the older half of the current evaluation window;
current period = the newer half. This project doesn't persist the raw
training-time feature/prediction distributions anywhere, so "drift vs.
training" can't be measured directly — this detects drift *since the
model went live and started accumulating settled predictions*, which is
still the actionable signal (a model whose live behavior is shifting
under it). This is an intentional, documented simplification — see
docs/RUNBOOKS.md.

PSI thresholds are the commonly-used industry rule-of-thumb bands, not
Bazaar-specific tuning: <0.10 no significant shift, 0.10-0.25 moderate
(flagged), >0.25 major (flagged). Automatic retraining is never
triggered from a single crossed threshold — see reliability_recommend.py.
"""
from __future__ import annotations

import numpy as np
from scipy import stats as scipy_stats

from market.models import PredictionSnapshot
from market.services import close_learn, ml_model
from market.services.reliability_metrics import compute_calibration

PSI_MODERATE = 0.10
PSI_MAJOR = 0.25
MIN_ROWS_PER_HALF = 15


def population_stability_index(reference: np.ndarray, current: np.ndarray, buckets: int = 10) -> float | None:
    """Standard PSI over quantile buckets of the reference distribution."""
    reference = np.asarray(reference, dtype=float)
    current = np.asarray(current, dtype=float)
    if len(reference) < 5 or len(current) < 5:
        return None
    quantiles = np.unique(np.percentile(reference, np.linspace(0, 100, buckets + 1)))
    if len(quantiles) < 3:
        return None
    ref_counts, _ = np.histogram(reference, bins=quantiles)
    cur_counts, _ = np.histogram(current, bins=quantiles)
    ref_pct = np.clip(ref_counts / max(len(reference), 1), 1e-4, None)
    cur_pct = np.clip(cur_counts / max(len(current), 1), 1e-4, None)
    return round(float(np.sum((cur_pct - ref_pct) * np.log(cur_pct / ref_pct))), 4)


def ks_test(reference: np.ndarray, current: np.ndarray) -> dict | None:
    reference = np.asarray(reference, dtype=float)
    current = np.asarray(current, dtype=float)
    if len(reference) < 5 or len(current) < 5:
        return None
    statistic, p_value = scipy_stats.ks_2samp(reference, current)
    return {"statistic": round(float(statistic), 4), "p_value": round(float(p_value), 4)}


def _split_half(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    ordered = sorted(rows, key=lambda r: r.get("data_cutoff_date"))
    mid = len(ordered) // 2
    return ordered[:mid], ordered[mid:]


def _psi_flag(psi: float | None) -> str | None:
    if psi is None:
        return None
    if psi > PSI_MAJOR:
        return "major"
    if psi > PSI_MODERATE:
        return "moderate"
    return None


def assess_feature_schema(model_version, current_feature_cols: list[str]) -> dict:
    trained_cols = set(model_version.feature_schema or []) if model_version else set()
    current_cols = set(current_feature_cols or [])
    missing = sorted(trained_cols - current_cols)
    added = sorted(current_cols - trained_cols)
    return {
        "trained_features": sorted(trained_cols),
        "current_features": sorted(current_cols),
        "missing_features": missing,
        "newly_introduced_features": added,
        "changed": bool(missing or added),
    }


def assess_drift(rows: list[dict], family: str, model_version=None) -> dict:
    """rows: settled PredictionSnapshot dicts for one (family, exchange,
    horizon) group across the full evaluation window — split-half
    happens internally."""
    n = len(rows)
    result: dict = {
        "reference_period": None,
        "current_period": None,
        "prediction_distribution": {},
        "outcome_distribution": {},
        "calibration_drift": None,
        "performance_drift": None,
        "feature_schema": {},
        "flags": [],
    }
    current_cols = ml_model.FEATURE_COLS if family == PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF else close_learn.FEATURE_COLS
    result["feature_schema"] = assess_feature_schema(model_version, current_cols)
    if result["feature_schema"]["changed"]:
        result["flags"].append(
            f"feature schema changed since this model version trained "
            f"(missing={result['feature_schema']['missing_features']}, "
            f"added={result['feature_schema']['newly_introduced_features']})"
        )

    if n < MIN_ROWS_PER_HALF * 2:
        result["note"] = f"Fewer than {MIN_ROWS_PER_HALF * 2} settled rows — split-half drift comparison skipped as unreliable."
        return result

    ref, cur = _split_half(rows)
    result["reference_period"] = {"start": str(ref[0]["data_cutoff_date"]), "end": str(ref[-1]["data_cutoff_date"]), "n": len(ref)}
    result["current_period"] = {"start": str(cur[0]["data_cutoff_date"]), "end": str(cur[-1]["data_cutoff_date"]), "n": len(cur)}

    is_classifier = family == PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF
    if is_classifier:
        ref_vals = np.array([r["predicted_probability"] for r in ref])
        cur_vals = np.array([r["predicted_probability"] for r in cur])
        ref_out = np.array([1 if r["outcome_class"] else 0 for r in ref])
        cur_out = np.array([1 if r["outcome_class"] else 0 for r in cur])
    else:
        ref_vals = np.array([r["predicted_return"] for r in ref])
        cur_vals = np.array([r["predicted_return"] for r in cur])
        ref_out = np.array([r["outcome_return"] for r in ref])
        cur_out = np.array([r["outcome_return"] for r in cur])

    pred_psi = population_stability_index(ref_vals, cur_vals)
    pred_ks = ks_test(ref_vals, cur_vals)
    result["prediction_distribution"] = {"psi": pred_psi, "ks": pred_ks}
    flag = _psi_flag(pred_psi)
    if flag:
        result["flags"].append(f"prediction-distribution drift ({flag}, PSI={pred_psi})")
    if pred_ks and pred_ks["p_value"] < 0.05:
        result["flags"].append(f"prediction distribution significantly different between halves (KS p={pred_ks['p_value']})")

    if is_classifier:
        outcome_psi = population_stability_index(ref_out, cur_out)
        ref_rate, cur_rate = float(np.mean(ref_out)), float(np.mean(cur_out))
        result["outcome_distribution"] = {"reference_rate": round(ref_rate, 4), "current_rate": round(cur_rate, 4), "psi": outcome_psi}
        if abs(cur_rate - ref_rate) > 0.15:
            result["flags"].append(f"outcome/class-balance shifted by {abs(cur_rate - ref_rate):.3f} between halves")

        ref_calib = compute_calibration(ref_out, ref_vals)
        cur_calib = compute_calibration(cur_out, cur_vals)
        ref_ece, cur_ece = ref_calib["expected_calibration_error"], cur_calib["expected_calibration_error"]
        result["calibration_drift"] = {"reference_ece": ref_ece, "current_ece": cur_ece}
        if ref_ece is not None and cur_ece is not None and (cur_ece - ref_ece) > 0.1:
            result["flags"].append(f"calibration error worsened from {ref_ece:.3f} to {cur_ece:.3f} between halves")

        ref_pred_cls = np.array([1 if r["predicted_class"] else 0 for r in ref])
        cur_pred_cls = np.array([1 if r["predicted_class"] else 0 for r in cur])
        ref_hit = float(np.mean(ref_pred_cls == ref_out))
        cur_hit = float(np.mean(cur_pred_cls == cur_out))
    else:
        ref_dir_hit = (ref_vals >= 0) == (ref_out >= 0)
        cur_dir_hit = (cur_vals >= 0) == (cur_out >= 0)
        ref_hit = float(np.mean(ref_dir_hit))
        cur_hit = float(np.mean(cur_dir_hit))

    result["performance_drift"] = {"reference_hit_rate": round(ref_hit, 4), "current_hit_rate": round(cur_hit, 4)}
    if (ref_hit - cur_hit) > 0.15:
        result["flags"].append(f"direction-hit rate degraded from {ref_hit:.3f} to {cur_hit:.3f} between halves")

    return result
