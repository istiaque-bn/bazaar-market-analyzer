"""Research-only forward-return challenger with stronger recent weighting.

The challenger is never activated or used by inference.  It uses a one-year
healthy training window, selects its algorithm on the following 60 calendar
days, and reports once on an untouched final 30-day test window.  Embargoes
separate every fit boundary from the forward 10-session label horizon.
"""
from __future__ import annotations

from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from django.conf import settings

from market.models import Exchange
from market.services.ml_model import (
    CANDIDATE_KINDS,
    EMBARGO_CALENDAR_DAYS,
    FEATURE_COLS,
    MIN_FOLD_TEST_ROWS,
    MIN_FOLD_TRAIN_ROWS,
    MODEL_PATH_BY_EXCHANGE,
    _binary_model,
    build_training_panel,
)
from market.services.ml_training import (
    apply_imputer,
    classification_metrics,
    fit_median_imputer,
    majority_class_baseline,
    new_version_tag,
    persistence_baseline,
    record_model_version,
    recency_weights,
    simple_market_baseline,
    skill_vs_baseline,
    zero_return_baseline,
)

MODEL_NAME = "forward_return_recent60"
MODEL_PATH = Path(settings.CACHE_DIR) / "forward_return_recent60_DSE.pkl"
TRAINING_LOOKBACK_DAYS = 365
VALIDATION_DAYS = 60
FINAL_TEST_DAYS = 30
RECENCY_HALF_LIFE_DAYS = 60


def _fit_and_score(train: pd.DataFrame, test: pd.DataFrame, kind: str, *, half_life_days: int) -> tuple[dict, object, object]:
    X_train = train[FEATURE_COLS].clip(-50, 50)
    X_test = test[FEATURE_COLS].clip(-50, 50)
    y_train = train["label"].to_numpy()
    y_test = test["label"].to_numpy()
    imputer = fit_median_imputer(X_train)
    X_train_i = apply_imputer(imputer, X_train)
    X_test_i = apply_imputer(imputer, X_test)
    model = _binary_model(kind)
    model.fit(
        X_train_i,
        y_train,
        sample_weight=recency_weights(train["date"], half_life_days=half_life_days, reference=train["date"].max()),
    )
    classes = list(model.classes_)
    proba = model.predict_proba(X_test_i)
    prob1 = proba[:, classes.index(1.0)] if 1.0 in classes else np.zeros(len(test))
    pred = (prob1 >= 0.5).astype(int)
    metrics = classification_metrics(y_test, pred, prob1)

    maj_pred, maj_prob = majority_class_baseline(y_train, len(test))
    zero_pred, zero_prob = zero_return_baseline(len(test))
    pers_pred, pers_prob = persistence_baseline(train["return_20d"], test["return_20d"])
    market_pred, market_prob = simple_market_baseline(y_train, train["exchange"], test["exchange"])
    baselines = {
        "majority_class": classification_metrics(y_test, maj_pred, maj_prob),
        "zero_return": classification_metrics(y_test, zero_pred, zero_prob),
        "persistence": classification_metrics(y_test, pers_pred, pers_prob),
        "simple_market": classification_metrics(y_test, market_pred, market_prob),
    }
    return {
        "metrics": metrics,
        "baselines": baselines,
        "skill_vs_baseline": {name: skill_vs_baseline(metrics, value) for name, value in baselines.items()},
    }, model, imputer


def _current_config() -> tuple[str, int | None]:
    path = MODEL_PATH_BY_EXCHANGE[Exchange.DSE]
    try:
        bundle = joblib.load(path)
        return bundle.get("model_kind") or "xgboost", bundle.get("rolling_days")
    except Exception:
        return "xgboost", None


def train_recent_weighted_challenger(*, limit_stocks: int = 120, persist: bool = True) -> dict:
    panel = build_training_panel(Exchange.DSE, limit_stocks=limit_stocks)
    if panel.empty:
        return {"ok": False, "error": "no healthy DSE training data"}

    latest = panel["date"].max()
    test_start = latest - pd.Timedelta(days=FINAL_TEST_DAYS)
    validation_start = test_start - pd.Timedelta(days=VALIDATION_DAYS)
    initial_train_end = validation_start - pd.Timedelta(days=EMBARGO_CALENDAR_DAYS)
    train_start = initial_train_end - pd.Timedelta(days=TRAINING_LOOKBACK_DAYS)
    final_train_end = test_start - pd.Timedelta(days=EMBARGO_CALENDAR_DAYS)

    initial_train = panel[(panel["date"] >= train_start) & (panel["date"] <= initial_train_end)].copy()
    validation = panel[(panel["date"] >= validation_start) & (panel["date"] < test_start)].copy()
    final_train = panel[(panel["date"] >= train_start) & (panel["date"] <= final_train_end)].copy()
    final_test = panel[panel["date"] >= test_start].copy()
    if min(len(initial_train), len(final_train)) < MIN_FOLD_TRAIN_ROWS or min(len(validation), len(final_test)) < MIN_FOLD_TEST_ROWS:
        return {"ok": False, "error": "insufficient rows in chronological train/validation/test windows"}

    validation_results = {}
    for kind in CANDIDATE_KINDS:
        scored, _, _ = _fit_and_score(initial_train, validation, kind, half_life_days=RECENCY_HALF_LIFE_DAYS)
        validation_results[kind] = scored
    selected_kind = max(
        validation_results,
        key=lambda kind: (
            validation_results[kind]["metrics"].get("balanced_accuracy") or -1,
            -(validation_results[kind]["metrics"].get("brier") or 999),
        ),
    )

    challenger, model, imputer = _fit_and_score(final_train, final_test, selected_kind, half_life_days=RECENCY_HALF_LIFE_DAYS)
    reference_kind, reference_window = _current_config()
    reference_train = final_train
    if reference_window:
        reference_train = reference_train[reference_train["date"] >= final_train_end - pd.Timedelta(days=reference_window)]
    reference, _, _ = _fit_and_score(reference_train, final_test, reference_kind, half_life_days=180)

    comparison = {}
    for key in ("accuracy", "balanced_accuracy", "precision", "recall", "brier"):
        new_value = challenger["metrics"].get(key)
        old_value = reference["metrics"].get(key)
        comparison[key] = round(new_value - old_value, 6) if new_value is not None and old_value is not None else None

    version = new_version_tag()
    result = {
        "ok": True,
        "model_name": MODEL_NAME,
        "version": version,
        "status": "experimental",
        "selected_kind": selected_kind,
        "reference_kind": reference_kind,
        "windows": {
            "training": [initial_train["date"].min().date().isoformat(), initial_train["date"].max().date().isoformat()],
            "validation": [validation["date"].min().date().isoformat(), validation["date"].max().date().isoformat()],
            "final_test": [final_test["date"].min().date().isoformat(), final_test["date"].max().date().isoformat()],
        },
        "rows": {"initial_train": len(initial_train), "validation": len(validation), "final_train": len(final_train), "final_test": len(final_test)},
        "validation_candidates": validation_results,
        "challenger": challenger,
        "current_reference": reference,
        "difference_challenger_minus_current": comparison,
    }
    if persist:
        joblib.dump(
            {
                "model": model,
                "imputer": imputer,
                "features": FEATURE_COLS,
                "version": version,
                "exchange_scope": Exchange.DSE,
                "status": "experimental",
                "research_design": result["windows"],
                "metrics": result,
            },
            MODEL_PATH,
        )
        record_model_version(
            model_name=MODEL_NAME,
            version=version,
            exchange_scope=Exchange.DSE,
            status="experimental",
            is_active=False,
            data_cutoff=final_train["date"].max().date(),
            feature_schema=FEATURE_COLS,
            train_rows=len(final_train),
            fold_metadata=[],
            metrics=result,
            file_path=str(MODEL_PATH),
            backup_path="",
            notes="Research challenger: 365d healthy history, 60d recency half-life, 60d validation, untouched 30d test; never auto-activated.",
        )
        result["path"] = str(MODEL_PATH)
    return result
