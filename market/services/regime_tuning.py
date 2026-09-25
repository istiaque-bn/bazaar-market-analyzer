"""Research-only, regime-routed forward-return challenger with a bounded
hyperparameter search.

Motivation
----------
The plain regime challenger (``management/commands/test_regime_model.py``)
over-predicts "up" in a bullish index window and lands *below* random on the
untouched test set.  This module tunes the levers that could plausibly fix
that -- model family, class weighting, recency half-life, tree depth/leaf,
per-regime decision threshold, and an abstention band -- but it does so
honestly:

* Every hyperparameter is chosen on a chronological **validation** window.
* The single selected configuration is judged **once** on an untouched final
  **test** window that no tuning ever looked at.  Optimising directly on the
  test window would inflate the reported number without improving future
  predictions, so we never do it.
* A minimum-coverage floor stops the search from "winning" by abstaining on
  everything except a handful of easy rows.

Nothing here is ever activated or used by live inference.  ``persist=True``
only records an ``experimental`` artifact for later reliability tracking.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from django.conf import settings
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression

from market.models import Exchange
from market.services.ml_model import (
    EMBARGO_CALENDAR_DAYS,
    FEATURE_COLS,
    build_training_panel,
)
from market.services.ml_training import (
    apply_imputer,
    classification_metrics,
    fit_median_imputer,
    majority_class_baseline,
    new_version_tag,
    recency_weights,
    record_model_version,
    skill_vs_baseline,
    zero_return_baseline,
)

MODEL_NAME = "forward_return_regime_tuned"
MODEL_PATH = Path(settings.CACHE_DIR) / "forward_return_regime_tuned_DSE.pkl"

TRAINING_LOOKBACK_DAYS = 365
VALIDATION_DAYS = 60
FINAL_TEST_DAYS = 30

# Per-regime minimum rows before that regime gets its own routed model.
MIN_REGIME_TRAIN_ROWS = 100
MIN_REGIME_EVAL_ROWS = 20

THRESHOLD_GRID = np.round(np.arange(0.30, 0.71, 0.05), 2)
BAND_GRID = (0.0, 0.05, 0.08, 0.12)

# The untuned reference the tuned challenger is compared against: the same
# defaults the plain regime command uses (logistic, balanced, 180d half-life,
# 0.5 threshold, 0.08 band).
REFERENCE_CONFIG = {
    "kind": "logistic",
    "class_weight": "balanced",
    "half_life_days": 180,
    "max_depth": None,
    "min_samples_leaf": None,
}
REFERENCE_THRESHOLD = 0.5
REFERENCE_BAND = 0.08


@dataclass(frozen=True)
class GridConfig:
    kind: str
    class_weight: str | None
    half_life_days: int
    max_depth: int | None = None
    min_samples_leaf: int | None = None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "class_weight": self.class_weight,
            "half_life_days": self.half_life_days,
            "max_depth": self.max_depth,
            "min_samples_leaf": self.min_samples_leaf,
        }


def _default_grid() -> list[GridConfig]:
    """Bounded outer grid.  Deliberately small: the point is a defensible
    sweep of the plausible levers, not an exhaustive search that would
    over-fit the validation window through sheer number of trials."""
    configs: list[GridConfig] = []
    for half_life in (60, 120, 180):
        for class_weight in ("balanced", None):
            configs.append(GridConfig("logistic", class_weight, half_life))
            for max_depth, min_leaf in ((6, 20), (8, 12)):
                configs.append(
                    GridConfig("random_forest", class_weight, half_life, max_depth, min_leaf)
                )
    return configs


def _make_model(cfg: GridConfig):
    if cfg.kind == "logistic":
        return LogisticRegression(
            max_iter=1500, class_weight=cfg.class_weight, random_state=42
        )
    return RandomForestClassifier(
        n_estimators=120,
        max_depth=cfg.max_depth,
        min_samples_leaf=cfg.min_samples_leaf,
        class_weight=cfg.class_weight,
        random_state=42,
        n_jobs=settings.ML_MAX_WORKERS,
    )


def _fit_regime_models(train: pd.DataFrame, cfg: GridConfig) -> dict:
    """One imputer+model per regime that has enough training rows."""
    models: dict[float, dict] = {}
    for regime in sorted(train["trend_regime"].unique()):
        sub = train[train["trend_regime"] == regime]
        if len(sub) < MIN_REGIME_TRAIN_ROWS or sub["label"].nunique() < 2:
            continue
        imputer = fit_median_imputer(sub[FEATURE_COLS])
        model = _make_model(cfg)
        model.fit(
            apply_imputer(imputer, sub[FEATURE_COLS]),
            sub["label"].to_numpy(),
            sample_weight=recency_weights(
                sub["date"], half_life_days=cfg.half_life_days, reference=sub["date"].max()
            ),
        )
        models[float(regime)] = {"imputer": imputer, "model": model}
    return models


def _predict_prob(models: dict, frame: pd.DataFrame) -> np.ndarray:
    """Route each row to its regime's model; rows in an unmodelled regime
    stay NaN so they are excluded rather than silently scored at 0.5."""
    out = np.full(len(frame), np.nan)
    for regime, bundle in models.items():
        mask = (frame["trend_regime"] == regime).to_numpy()
        if not mask.any():
            continue
        model = bundle["model"]
        proba = model.predict_proba(apply_imputer(bundle["imputer"], frame.loc[mask, FEATURE_COLS]))
        classes = list(model.classes_)
        prob1 = proba[:, classes.index(1.0)] if 1.0 in classes else np.zeros(int(mask.sum()))
        out[mask] = prob1
    return out


def _tune_thresholds(val: pd.DataFrame, prob: np.ndarray, modelled_regimes) -> dict[float, float]:
    """Per-regime threshold that maximises that regime's validation balanced
    accuracy.  Judged on validation only."""
    thresholds: dict[float, float] = {}
    for regime in modelled_regimes:
        mask = ((val["trend_regime"] == regime) & ~np.isnan(prob)).to_numpy()
        if mask.sum() < MIN_REGIME_EVAL_ROWS:
            thresholds[regime] = 0.5
            continue
        y = val.loc[mask, "label"].to_numpy()
        p = prob[mask]
        best_t, best_score = 0.5, -1.0
        for t in THRESHOLD_GRID:
            score = classification_metrics(y, (p >= t).astype(int), p).get("balanced_accuracy")
            if score is not None and score > best_score:
                best_t, best_score = float(t), score
        thresholds[regime] = best_t
    return thresholds


def _apply(frame: pd.DataFrame, prob: np.ndarray, thresholds: dict[float, float], band: float):
    """Return (mask_scored, y, pred, prob, covered_mask) for a frame.

    ``covered_mask`` is over the *scored* rows: a row is covered when its
    probability sits at least ``band`` away from that regime's threshold.
    """
    scored = ~np.isnan(prob)
    thr = np.array([thresholds.get(float(r), 0.5) for r in frame["trend_regime"]])
    y = frame["label"].to_numpy()
    pred = (prob >= thr).astype(int)
    confidence = np.abs(prob - thr)
    covered = scored & (confidence >= band)
    return scored, y, pred, prob, covered


_EMPTY_METRICS = {
    "n": 0,
    "accuracy": None,
    "balanced_accuracy": None,
    "precision": None,
    "recall": None,
    "direction_hit_rate": None,
    "positive_rate_true": None,
    "positive_rate_pred": None,
    "brier": None,
}


def _covered_metrics(y, pred, prob, covered) -> tuple[dict, float]:
    if covered.sum() == 0:
        return dict(_EMPTY_METRICS), 0.0
    metrics = classification_metrics(y[covered], pred[covered], prob[covered])
    coverage = float(covered.sum()) / float((~np.isnan(prob)).sum() or 1)
    return metrics, round(coverage, 4)


def _select_band(val: pd.DataFrame, prob: np.ndarray, thresholds, *, min_coverage: float):
    """Choose the abstention band that maximises covered validation balanced
    accuracy while keeping coverage >= ``min_coverage``.  If no band clears
    the floor, fall back to the widest coverage available (band 0.0)."""
    best = None
    for band in BAND_GRID:
        _, y, pred, p, covered = _apply(val, prob, thresholds, band)
        metrics, coverage = _covered_metrics(y, pred, p, covered)
        ba = metrics.get("balanced_accuracy")
        if coverage < min_coverage or ba is None:
            continue
        key = (ba, -(metrics.get("brier") or 999), coverage)
        if best is None or key > best[0]:
            best = (key, band, metrics, coverage)
    if best is None:
        _, y, pred, p, covered = _apply(val, prob, thresholds, 0.0)
        metrics, coverage = _covered_metrics(y, pred, p, covered)
        return 0.0, metrics, coverage, False
    return best[1], best[2], best[3], True


def _baselines_on_covered(y, pred, prob, covered) -> dict:
    if covered.sum() == 0:
        return {}
    yc = y[covered]
    n = int(covered.sum())
    maj_pred, maj_prob = majority_class_baseline(yc, n)
    zero_pred, zero_prob = zero_return_baseline(n)
    return {
        "majority_class": classification_metrics(yc, maj_pred, maj_prob),
        "zero_return": classification_metrics(yc, zero_pred, zero_prob),
    }


def _chronological_windows(panel: pd.DataFrame) -> dict:
    latest = panel["date"].max()
    test_start = latest - pd.Timedelta(days=FINAL_TEST_DAYS)
    val_start = test_start - pd.Timedelta(days=VALIDATION_DAYS)
    initial_train_end = val_start - pd.Timedelta(days=EMBARGO_CALENDAR_DAYS)
    train_start = initial_train_end - pd.Timedelta(days=TRAINING_LOOKBACK_DAYS)
    final_train_end = test_start - pd.Timedelta(days=EMBARGO_CALENDAR_DAYS)
    return {
        "initial_train": panel[(panel["date"] >= train_start) & (panel["date"] <= initial_train_end)].copy(),
        "validation": panel[(panel["date"] >= val_start) & (panel["date"] < test_start)].copy(),
        "final_train": panel[(panel["date"] >= train_start) & (panel["date"] <= final_train_end)].copy(),
        "test": panel[panel["date"] >= test_start].copy(),
    }


def _regimes_with_rows(frame: pd.DataFrame, minimum: int) -> set:
    counts = frame["trend_regime"].value_counts()
    return {float(r) for r, c in counts.items() if c >= minimum}


def tune_regime_challenger(
    *,
    exchange: str = Exchange.DSE,
    limit_stocks: int = 120,
    min_coverage: float = 0.30,
    persist: bool = False,
) -> dict:
    panel = build_training_panel(exchange, limit_stocks=limit_stocks)
    if panel.empty:
        return {"ok": False, "error": "no healthy training data"}

    w = _chronological_windows(panel)
    initial_train, validation, final_train, test = (
        w["initial_train"], w["validation"], w["final_train"], w["test"]
    )
    if min(len(initial_train), len(final_train)) < MIN_REGIME_TRAIN_ROWS or min(len(validation), len(test)) < MIN_REGIME_EVAL_ROWS:
        return {"ok": False, "error": "insufficient rows in chronological train/validation/test windows"}

    # A regime present in the untouched test but never tunable on validation
    # would be scored with an untuned default threshold -- silently guessing
    # on data the search never learned to handle. Fail loudly instead.
    test_regimes = _regimes_with_rows(test, MIN_REGIME_EVAL_ROWS)
    val_regimes = _regimes_with_rows(validation, MIN_REGIME_EVAL_ROWS)
    missing = sorted(test_regimes - val_regimes)
    if missing:
        return {
            "ok": False,
            "error": "validation lacks regimes present in final test",
            "missing_regimes": missing,
            "validation_distribution": validation["trend_regime"].value_counts().to_dict(),
            "test_distribution": test["trend_regime"].value_counts().to_dict(),
        }

    # --- Search: fit on initial_train, tune every lever on validation. ---
    trials = []
    best = None
    for cfg in _default_grid():
        models = _fit_regime_models(initial_train, cfg)
        if not models:
            continue
        val_prob = _predict_prob(models, validation)
        thresholds = _tune_thresholds(validation, val_prob, list(models.keys()))
        band, val_metrics, val_coverage, cleared_floor = _select_band(
            validation, val_prob, thresholds, min_coverage=min_coverage
        )
        ba = val_metrics.get("balanced_accuracy")
        trial = {
            "config": cfg.as_dict(),
            "thresholds": {str(k): v for k, v in thresholds.items()},
            "band": band,
            "validation": val_metrics,
            "validation_coverage": val_coverage,
            "cleared_coverage_floor": cleared_floor,
        }
        trials.append(trial)
        if ba is None or not cleared_floor:
            continue
        key = (ba, -(val_metrics.get("brier") or 999), val_coverage)
        if best is None or key > best["key"]:
            best = {"key": key, "cfg": cfg, "thresholds": thresholds, "band": band, "trial": trial}

    if best is None:
        return {
            "ok": False,
            "error": "no configuration cleared the coverage floor on validation",
            "min_coverage": min_coverage,
            "trials": trials,
        }

    # --- Judge the single winner ONCE on the untouched test window. ---
    final_models = _fit_regime_models(final_train, best["cfg"])
    test_prob = _predict_prob(final_models, test)
    scored, y, pred, prob, covered = _apply(test, test_prob, best["thresholds"], best["band"])
    all_metrics = (
        classification_metrics(y[scored], pred[scored], prob[scored])
        if scored.sum() else dict(_EMPTY_METRICS)
    )
    covered_metrics, coverage = _covered_metrics(y, pred, prob, covered)
    baselines = _baselines_on_covered(y, pred, prob, covered)

    # Untuned reference on the same test window, for an honest delta.
    ref_cfg = GridConfig(**REFERENCE_CONFIG)
    ref_models = _fit_regime_models(final_train, ref_cfg)
    ref_prob = _predict_prob(ref_models, test)
    ref_thresholds = {r: REFERENCE_THRESHOLD for r in ref_models}
    _, ry, rpred, rprob, rcovered = _apply(test, ref_prob, ref_thresholds, REFERENCE_BAND)
    ref_covered_metrics, ref_coverage = _covered_metrics(ry, rpred, rprob, rcovered)

    difference = {}
    for key in ("balanced_accuracy", "accuracy", "precision", "recall", "brier"):
        new_v, old_v = covered_metrics.get(key), ref_covered_metrics.get(key)
        difference[key] = round(new_v - old_v, 6) if new_v is not None and old_v is not None else None

    version = new_version_tag()
    result = {
        "ok": True,
        "model_name": MODEL_NAME,
        "version": version,
        "status": "experimental",
        "exchange": exchange,
        "min_coverage": min_coverage,
        "selected_config": best["cfg"].as_dict(),
        "selected_thresholds": {str(k): v for k, v in best["thresholds"].items()},
        "selected_band": best["band"],
        "windows": {
            "initial_train": [initial_train["date"].min().date().isoformat(), initial_train["date"].max().date().isoformat()],
            "validation": [validation["date"].min().date().isoformat(), validation["date"].max().date().isoformat()],
            "final_train": [final_train["date"].min().date().isoformat(), final_train["date"].max().date().isoformat()],
            "test": [test["date"].min().date().isoformat(), test["date"].max().date().isoformat()],
        },
        "regimes_modelled": sorted(final_models.keys()),
        "validation_best": best["trial"],
        "test_all": all_metrics,
        "test_covered": covered_metrics,
        "test_coverage": coverage,
        "test_covered_baselines": baselines,
        "test_skill_vs_baseline": {
            name: skill_vs_baseline(covered_metrics, value) for name, value in baselines.items()
        },
        "reference_untuned": {
            "config": ref_cfg.as_dict(),
            "test_covered": ref_covered_metrics,
            "test_coverage": ref_coverage,
        },
        "difference_tuned_minus_reference": difference,
        "trials_evaluated": len(trials),
    }

    if persist:
        joblib.dump(
            {
                "regime_models": final_models,
                "thresholds": best["thresholds"],
                "band": best["band"],
                "config": best["cfg"].as_dict(),
                "features": FEATURE_COLS,
                "version": version,
                "exchange_scope": exchange,
                "status": "experimental",
                "metrics": result,
            },
            MODEL_PATH,
        )
        record_model_version(
            model_name=MODEL_NAME,
            version=version,
            exchange_scope=exchange,
            status="experimental",
            is_active=False,
            data_cutoff=final_train["date"].max().date(),
            feature_schema=FEATURE_COLS,
            train_rows=len(final_train),
            fold_metadata=[],
            metrics=result,
            file_path=str(MODEL_PATH),
            backup_path="",
            notes=(
                "Research challenger: regime-routed, bounded grid search over "
                "family/class-weight/half-life/depth + per-regime threshold + "
                "abstention band; tuned on validation, judged once on untouched "
                "30d test; never auto-activated."
            ),
        )
        result["path"] = str(MODEL_PATH)
    return result
