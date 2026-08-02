"""
ML Reliability Monitor — metrics.

Reuses market.services.ml_training's baseline metric functions
(classification_metrics/regression_metrics/skill_vs_baseline/
regression_skill) — the same functions the walk-forward deployment gate
itself is scored with — and adds the extra diagnostics this monitor
needs on top: ROC AUC, log loss, calibration, economic diagnostics, and
bootstrap confidence intervals.

No claim here implies statistical accuracy guarantees profitability —
economic diagnostics are a simple documented sanity check (see
compute_economic_diagnostics), not a portfolio simulation; see
market.services.backtest_engine for that.
"""
from __future__ import annotations

import numpy as np
from sklearn.metrics import log_loss as sk_log_loss
from sklearn.metrics import mean_squared_error, roc_auc_score

from market.models import PredictionSnapshot, ReliabilityAssessment
from market.services.backtest_engine import CostConfig
from market.services.ml_training import classification_metrics as _classification_metrics
from market.services.ml_training import regression_metrics as _regression_metrics
from market.services.ml_training import regression_skill as _regression_skill
from market.services.ml_training import skill_vs_baseline as _skill_vs_baseline

WINDOW_SIZES = [30, 90, 180, 365]
CALIBRATION_BUCKETS = 10
MIN_SAMPLES_INSUFFICIENT = 30
MIN_SAMPLES_WATCH = 60
HIGH_CALIBRATION_ERROR = 0.15


# ---------------------------------------------------------------------------
# Classifier metrics (forward_return_rf)
# ---------------------------------------------------------------------------


def compute_calibration(y_true: np.ndarray, y_prob: np.ndarray) -> dict:
    """Reliability buckets: predicted probability vs. observed frequency,
    binned into CALIBRATION_BUCKETS equal-width buckets. Expected
    Calibration Error is the sample-weighted mean absolute gap between
    predicted and observed within each non-empty bucket."""
    if len(y_true) == 0:
        return {"expected_calibration_error": None, "buckets": []}
    edges = np.linspace(0.0, 1.0, CALIBRATION_BUCKETS + 1)
    buckets = []
    ece = 0.0
    n = len(y_true)
    for i in range(CALIBRATION_BUCKETS):
        lo, hi = edges[i], edges[i + 1]
        mask = (y_prob >= lo) & (y_prob <= hi) if i == CALIBRATION_BUCKETS - 1 else (y_prob >= lo) & (y_prob < hi)
        count = int(mask.sum())
        if count == 0:
            continue
        avg_pred = float(y_prob[mask].mean())
        actual_freq = float(y_true[mask].mean())
        buckets.append(
            {
                "bucket": f"{lo:.1f}-{hi:.1f}",
                "n": count,
                "avg_predicted_probability": round(avg_pred, 4),
                "actual_frequency": round(actual_freq, 4),
            }
        )
        ece += (count / n) * abs(avg_pred - actual_freq)
    return {"expected_calibration_error": round(ece, 4) if buckets else None, "buckets": buckets}


def compute_classifier_metrics(rows: list[dict]) -> dict:
    """rows: dicts with predicted_class(bool), predicted_probability(float),
    rule_baseline_class(bool|None), naive_baseline_class(bool|None),
    outcome_class(bool) — only settled rows should be passed in."""
    n = len(rows)
    if n == 0:
        return {"n": 0}

    y_true = np.array([1 if r["outcome_class"] else 0 for r in rows])
    y_pred = np.array([1 if r["predicted_class"] else 0 for r in rows])
    y_prob = np.array([float(r["predicted_probability"]) if r["predicted_probability"] is not None else 0.5 for r in rows])
    y_prob_clipped = np.clip(y_prob, 1e-6, 1 - 1e-6)

    model = _classification_metrics(y_true, y_pred, y_prob)
    precision, recall = model.get("precision"), model.get("recall")
    model["f1"] = round(2 * precision * recall / (precision + recall), 4) if precision and recall and (precision + recall) > 0 else 0.0

    model["roc_auc"] = None
    if len(set(y_true.tolist())) > 1:
        try:
            model["roc_auc"] = round(float(roc_auc_score(y_true, y_prob)), 4)
        except ValueError:
            model["roc_auc"] = None

    try:
        model["log_loss"] = round(float(sk_log_loss(y_true, y_prob_clipped, labels=[0, 1])), 4)
    except ValueError:
        model["log_loss"] = None

    calib = compute_calibration(y_true, y_prob)
    model["calibration_error"] = calib["expected_calibration_error"]
    model["calibration_buckets"] = calib["buckets"]

    baselines: dict = {}
    skill: dict = {}
    rule_rows = [r for r in rows if r.get("rule_baseline_class") is not None]
    if rule_rows:
        ry_true = np.array([1 if r["outcome_class"] else 0 for r in rule_rows])
        ry_pred = np.array([1 if r["rule_baseline_class"] else 0 for r in rule_rows])
        rule_metrics = _classification_metrics(ry_true, ry_pred, None)
        baselines["rule_based"] = rule_metrics
        skill["rule_based"] = _skill_vs_baseline(model, rule_metrics)

    naive_rows = [r for r in rows if r.get("naive_baseline_class") is not None]
    if naive_rows:
        ny_true = np.array([1 if r["outcome_class"] else 0 for r in naive_rows])
        ny_pred = np.array([1 if r["naive_baseline_class"] else 0 for r in naive_rows])
        naive_metrics = _classification_metrics(ny_true, ny_pred, None)
        baselines["naive_majority_class"] = naive_metrics
        skill["naive_majority_class"] = _skill_vs_baseline(model, naive_metrics)

    return {"n": n, "model": model, "baselines": baselines, "skill_vs_baseline": skill}


# ---------------------------------------------------------------------------
# Regressor metrics (next_close_rf)
# ---------------------------------------------------------------------------


def compute_regressor_metrics(rows: list[dict]) -> dict:
    """rows: dicts with predicted_return(float), rule_baseline_return
    (float|None), naive_baseline_return(float|None), outcome_return(float)
    — only settled rows should be passed in."""
    n = len(rows)
    if n == 0:
        return {"n": 0}

    y_true = np.array([float(r["outcome_return"]) for r in rows])
    y_pred = np.array([float(r["predicted_return"]) for r in rows])

    model = _regression_metrics(y_true, y_pred)
    abs_err = np.abs(y_pred - y_true)
    model["rmse"] = round(float(np.sqrt(mean_squared_error(y_true, y_pred))), 6)
    model["median_absolute_error"] = round(float(np.median(abs_err)), 6)
    model["bias"] = round(float(np.mean(y_pred - y_true)), 6)

    # Returns legitimately sit near zero, where a plain MAPE blows up —
    # symmetric MAPE avoids dividing by a near-zero denominator.
    denom = (np.abs(y_pred) + np.abs(y_true)) / 2
    safe = denom > 1e-6
    model["smape"] = round(float(np.mean(abs_err[safe] / denom[safe])) * 100, 4) if safe.any() else None

    if n >= 2 and np.std(y_true) > 1e-12 and np.std(y_pred) > 1e-12:
        model["correlation"] = round(float(np.corrcoef(y_true, y_pred)[0, 1]), 4)
    else:
        model["correlation"] = None

    baselines: dict = {}
    skill: dict = {}
    rule_rows = [r for r in rows if r.get("rule_baseline_return") is not None]
    if rule_rows:
        ry_true = np.array([float(r["outcome_return"]) for r in rule_rows])
        ry_pred = np.array([float(r["rule_baseline_return"]) for r in rule_rows])
        rule_metrics = _regression_metrics(ry_true, ry_pred)
        baselines["rule_based_persistence"] = rule_metrics
        skill["rule_based_persistence"] = _regression_skill(model, rule_metrics)

    naive_rows = [r for r in rows if r.get("naive_baseline_return") is not None]
    if naive_rows:
        ny_true = np.array([float(r["outcome_return"]) for r in naive_rows])
        ny_pred = np.array([float(r["naive_baseline_return"]) for r in naive_rows])
        naive_metrics = _regression_metrics(ny_true, ny_pred)
        baselines["naive_zero_return"] = naive_metrics
        skill["naive_zero_return"] = _regression_skill(model, naive_metrics)

    return {"n": n, "model": model, "baselines": baselines, "skill_vs_baseline": skill}


# ---------------------------------------------------------------------------
# Economic diagnostics (shared)
# ---------------------------------------------------------------------------


def _predicted_direction(row: dict, family: str) -> bool:
    if family == PredictionSnapshot.ModelFamily.NEXT_CLOSE_RF:
        return (row.get("predicted_return") or 0) > 0
    return bool(row.get("predicted_class"))


def compute_economic_diagnostics(rows: list[dict], family: str) -> dict:
    """Simple, explicitly documented evaluation policy: go long (unit
    position, no shorting) when the model's predicted direction is
    positive, otherwise stay flat. One position per settled observation,
    held from data_cutoff to target_date, chronologically compounded.
    Costs reuse BacktestRun's existing per-round-trip assumptions
    (market.services.backtest_engine.CostConfig) at 1x/1.5x/2x. This is a
    diagnostic sanity check, not a portfolio simulation — see BacktestRun
    for a real portfolio-level backtest. Statistical accuracy above does
    NOT imply this is profitable after real-world execution."""
    n = len(rows)
    if n == 0:
        return {"n": 0}

    cost = CostConfig()
    round_trip_cost_frac = 2 * (cost.fee_pct + cost.adverse_price_pct)  # buy + sell, each side

    ordered = sorted(rows, key=lambda r: r.get("target_date") or r.get("data_cutoff_date"))
    is_long = np.array([_predicted_direction(r, family) for r in ordered])
    gross = np.array([float(r["outcome_return"]) if long else 0.0 for r, long in zip(ordered, is_long)])
    benchmark = np.array([float(r["outcome_return"]) for r in ordered])  # always-long buy & hold

    turnover = float(is_long.mean())

    def _summary(multiplier: float) -> dict:
        net = np.where(is_long, gross - round_trip_cost_frac * multiplier, 0.0)
        equity = np.cumprod(1 + net)
        running_max = np.maximum.accumulate(equity)
        drawdown = np.where(running_max > 0, (equity - running_max) / running_max, 0.0)
        return {
            "net_total_return_pct": round((float(equity[-1]) - 1) * 100, 4) if len(equity) else 0.0,
            "max_drawdown_pct": round(float(drawdown.min()) * 100, 4) if len(drawdown) else 0.0,
            "estimated_total_cost_pct": round(float(is_long.sum()) * round_trip_cost_frac * multiplier * 100, 4),
        }

    benchmark_equity = np.cumprod(1 + benchmark)
    benchmark_total_return_pct = round((float(benchmark_equity[-1]) - 1) * 100, 4) if len(benchmark_equity) else 0.0
    at_1x = _summary(1.0)

    return {
        "n": n,
        "policy": (
            "Long (unit position) when predicted direction is positive, else flat; no shorting. "
            "One position per observation, held from data_cutoff to target_date, chronologically compounded. "
            "Statistical accuracy does not imply profitability after real-world execution."
        ),
        "trades_executed": int(is_long.sum()),
        "turnover": round(turnover, 4),
        "gross_mean_return_pct": round(float(np.mean(gross)) * 100, 4),
        "benchmark_buy_hold_total_return_pct": benchmark_total_return_pct,
        "cost_assumption_pct_round_trip": round(round_trip_cost_frac * 100, 4),
        "at_1x_cost": at_1x,
        "at_1_5x_cost": _summary(1.5),
        "at_2x_cost": _summary(2.0),
        "benchmark_relative_return_pct": round(at_1x["net_total_return_pct"] - benchmark_total_return_pct, 4),
    }


# ---------------------------------------------------------------------------
# Confidence intervals
# ---------------------------------------------------------------------------


def bootstrap_ci(values: list[float | None], statistic_fn=np.mean, n_boot: int = 1000, seed: int = 42, alpha: float = 0.05) -> dict:
    """i.i.d. resampling bootstrap CI, deterministic given `seed`.

    LIMITATION: prediction snapshots are not strictly independent
    (overlapping forecast horizons, market-wide co-movement on a given
    day), so this understates true uncertainty vs. a full time-series-
    aware block bootstrap. Documented honestly rather than presented as a
    rigorous interval — see docs/RUNBOOKS.md."""
    arr = np.asarray([v for v in values if v is not None], dtype=float)
    if len(arr) < 2:
        return {"low": None, "high": None, "n_boot": 0, "method": "bootstrap_iid"}
    rng = np.random.default_rng(seed)
    n = len(arr)
    stats = np.empty(n_boot)
    for i in range(n_boot):
        sample = arr[rng.integers(0, n, n)]
        stats[i] = statistic_fn(sample)
    lo, hi = np.percentile(stats, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return {"low": round(float(lo), 6), "high": round(float(hi), 6), "n_boot": n_boot, "method": "bootstrap_iid"}


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def determine_status(sample_count: int, skill_vs_naive: float | None, calibration_error: float | None, drift_flags: list[str]) -> tuple[str, list[str]]:
    """Never a single opaque score — status is a small enum plus the
    concrete reasons that produced it, each citing the metric involved."""
    reasons: list[str] = []
    if sample_count < MIN_SAMPLES_INSUFFICIENT:
        reasons.append(
            f"Only {sample_count} settled predictions in this window (need >= {MIN_SAMPLES_INSUFFICIENT}) — "
            "insufficient evidence to assess reliability."
        )
        return ReliabilityAssessment.Status.INSUFFICIENT_DATA, reasons

    if skill_vs_naive is None:
        reasons.append("Skill vs. naive baseline could not be computed for this window.")
        return ReliabilityAssessment.Status.WATCH, reasons

    status = ReliabilityAssessment.Status.HEALTHY
    if skill_vs_naive <= 0:
        reasons.append(f"Skill vs. naive baseline is not positive ({skill_vs_naive:.4f}) — no demonstrated edge over the naive baseline.")
        status = ReliabilityAssessment.Status.CRITICAL if sample_count >= MIN_SAMPLES_WATCH else ReliabilityAssessment.Status.DEGRADED
    elif sample_count < MIN_SAMPLES_WATCH:
        reasons.append(f"Only {sample_count} settled predictions — treating positive skill as provisional until more accumulate.")
        status = ReliabilityAssessment.Status.WATCH

    if calibration_error is not None and calibration_error > HIGH_CALIBRATION_ERROR:
        reasons.append(f"Calibration error is high ({calibration_error:.3f}) — predicted probabilities don't track observed frequencies well.")
        if status == ReliabilityAssessment.Status.HEALTHY:
            status = ReliabilityAssessment.Status.WATCH

    if drift_flags:
        reasons.append("Drift detected: " + "; ".join(drift_flags))
        if status == ReliabilityAssessment.Status.HEALTHY:
            status = ReliabilityAssessment.Status.WATCH

    if not reasons:
        reasons.append(
            f"Skill vs. naive baseline is positive ({skill_vs_naive:.4f}) over {sample_count} settled predictions; "
            "no drift or calibration concerns detected."
        )

    return status, reasons
