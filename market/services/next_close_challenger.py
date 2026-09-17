"""Research-only selective next-close challenger.

This module has deliberately no dependency on ``MLModelVersion`` and no
production-serving hook.  It predicts a return only when a robust regressor's
signal clears a dead-band; otherwise it predicts the hard-to-beat DSE baseline
of no change.  Model/config selection is performed on chronological
walk-forward predictions and the final 90 calendar days are touched once for
an honest holdout verdict.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

from market.models import Exchange
from market.services.close_learn import (
    EMBARGO_CALENDAR_DAYS,
    FEATURE_COLS,
    MIN_FOLD_TEST_ROWS,
    MIN_FOLD_TRAIN_ROWS,
    _build_next_close_panel,
)
from market.services.ml_training import (
    apply_imputer,
    fit_median_imputer,
    recency_weights,
    walk_forward_folds,
)
from market.services.next_close_research import split_final_holdout


@dataclass(frozen=True)
class ChallengerConfig:
    rolling_days: int = 365
    learning_rate: float = 0.04
    max_iter: int = 140
    max_leaf_nodes: int = 15
    min_samples_leaf: int = 60
    l2_regularization: float = 2.0
    shrinkage: float = 0.5
    deadband: float = 0.002


# Frozen before live shadow collection. Changes require a new candidate name
# so evidence from different policies is never mixed.
DEFAULT_CONFIG = ChallengerConfig()
CANDIDATE_NAME = "shadow_selective_hgb_v1"
MIN_RESEARCH_SKILL = 0.02
MIN_HOLDOUT_SKILL = 0.02
MIN_DIRECTION_HIT_RATE = 0.50


def _new_model(config: ChallengerConfig) -> HistGradientBoostingRegressor:
    return HistGradientBoostingRegressor(
        loss="absolute_error",
        learning_rate=config.learning_rate,
        max_iter=config.max_iter,
        max_leaf_nodes=config.max_leaf_nodes,
        min_samples_leaf=config.min_samples_leaf,
        l2_regularization=config.l2_regularization,
        random_state=42,
    )


def _selective(raw: np.ndarray, config: ChallengerConfig) -> np.ndarray:
    pred = np.clip(np.asarray(raw, dtype=float) * config.shrinkage, -0.12, 0.12)
    return np.where(np.abs(pred) >= config.deadband, pred, 0.0)


def _metrics(actual: np.ndarray, predicted: np.ndarray) -> dict:
    actual = np.asarray(actual, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    if not len(actual):
        return {"n": 0, "skill_vs_naive": None, "direction_hit_rate": None, "coverage": 0.0}
    model_mae = float(np.mean(np.abs(predicted - actual)))
    naive_mae = float(np.mean(np.abs(actual)))
    emitted = np.abs(predicted) > 1e-12
    directional = emitted & (np.abs(actual) > 1e-12)
    return {
        "n": int(len(actual)),
        "model_mae_return": round(model_mae, 6),
        "naive_mae_return": round(naive_mae, 6),
        "skill_vs_naive": None if naive_mae <= 1e-12 else round(1.0 - model_mae / naive_mae, 4),
        "direction_hit_rate": (
            round(float(np.mean(np.sign(predicted[directional]) == np.sign(actual[directional]))), 4)
            if directional.any()
            else None
        ),
        "coverage": round(float(emitted.mean()), 4),
    }


def _fit_predict(train: pd.DataFrame, test: pd.DataFrame, config: ChallengerConfig) -> np.ndarray:
    train_start = pd.Timestamp(train["date"].max()) - pd.Timedelta(days=config.rolling_days)
    train = train[train["date"] >= train_start]
    X_train = train[FEATURE_COLS].clip(-50, 50)
    X_test = test[FEATURE_COLS].clip(-50, 50)
    imputer = fit_median_imputer(X_train)
    X_train_i = apply_imputer(imputer, X_train)
    X_test_i = apply_imputer(imputer, X_test)
    model = _new_model(config)
    model.fit(
        X_train_i,
        train["fwd_ret_1"].clip(-0.12, 0.12).to_numpy(),
        sample_weight=recency_weights(train["date"], reference=train["date"].max()),
    )
    return _selective(model.predict(X_test_i), config)


def walk_forward_evaluate(panel: pd.DataFrame, config: ChallengerConfig = DEFAULT_CONFIG) -> dict:
    folds = walk_forward_folds(panel["date"], n_folds=3, embargo_days=EMBARGO_CALENDAR_DAYS)
    rows = []
    all_actual: list[float] = []
    all_predicted: list[float] = []
    for fold in folds:
        train = panel[panel["date"] <= fold.train_end]
        test = panel[(panel["date"] >= fold.test_start) & (panel["date"] <= fold.test_end)]
        if len(train) < MIN_FOLD_TRAIN_ROWS or len(test) < MIN_FOLD_TEST_ROWS:
            continue
        actual = test["fwd_ret_1"].clip(-0.12, 0.12).to_numpy()
        predicted = _fit_predict(train, test, config)
        fold_metrics = _metrics(actual, predicted)
        rows.append(
            {
                "fold": fold.fold,
                "train_end": fold.train_end.date().isoformat(),
                "test_start": fold.test_start.date().isoformat(),
                "test_end": fold.test_end.date().isoformat(),
                **fold_metrics,
            }
        )
        all_actual.extend(actual.tolist())
        all_predicted.extend(predicted.tolist())
    return {
        "ok": bool(rows),
        "config": asdict(config),
        "metrics": _metrics(np.asarray(all_actual), np.asarray(all_predicted)),
        "folds": rows,
    }


def evaluate_locked_holdout(panel: pd.DataFrame, config: ChallengerConfig = DEFAULT_CONFIG) -> dict:
    research, holdout = split_final_holdout(panel)
    if len(research) < MIN_FOLD_TRAIN_ROWS or len(holdout) < MIN_FOLD_TEST_ROWS:
        return {"ok": False, "error": "not enough rows for a locked final holdout"}
    research_result = walk_forward_evaluate(research, config)
    if not research_result["ok"]:
        return {"ok": False, "error": "research walk-forward evaluation failed", "research": research_result}
    predicted = _fit_predict(research, holdout, config)
    holdout_metrics = _metrics(holdout["fwd_ret_1"].clip(-0.12, 0.12).to_numpy(), predicted)
    fold_skills = [row.get("skill_vs_naive") for row in research_result["folds"]]
    ready_for_shadow = bool(
        (research_result["metrics"].get("skill_vs_naive") or 0) >= MIN_RESEARCH_SKILL
        and sum(skill is not None and skill > 0 for skill in fold_skills) >= 2
        and (holdout_metrics.get("skill_vs_naive") or 0) >= MIN_HOLDOUT_SKILL
        and (holdout_metrics.get("direction_hit_rate") or 0) > MIN_DIRECTION_HIT_RATE
    )
    return {
        "ok": True,
        "candidate": CANDIDATE_NAME,
        "config": asdict(config),
        "research": research_result,
        "holdout": holdout_metrics,
        "ready_for_shadow": ready_for_shadow,
        "ready_for_production": False,
        "production_blocker": (
            "must first clear the 2% research and holdout MAE-skill gates, then requires positive live shadow "
            "evidence; automatic promotion is intentionally unsupported"
        ),
    }


def train_for_shadow(
    *, exchange: str = Exchange.DSE, limit_stocks: int = 80, config: ChallengerConfig = DEFAULT_CONFIG
):
    panel = _build_next_close_panel(exchange, limit_stocks=limit_stocks)
    if len(panel) < MIN_FOLD_TRAIN_ROWS:
        return None
    cutoff = pd.Timestamp(panel["date"].max()) - pd.Timedelta(days=config.rolling_days)
    panel = panel[panel["date"] >= cutoff]
    X = panel[FEATURE_COLS].clip(-50, 50)
    imputer = fit_median_imputer(X)
    X_i = apply_imputer(imputer, X)
    model = _new_model(config)
    model.fit(
        X_i,
        panel["fwd_ret_1"].clip(-0.12, 0.12).to_numpy(),
        sample_weight=recency_weights(panel["date"], reference=panel["date"].max()),
    )
    return imputer, model, config


def predict(bundle, features: dict) -> float:
    imputer, model, config = bundle
    row = pd.DataFrame([{c: (0.0 if features.get(c) is None else features.get(c)) for c in FEATURE_COLS}])
    return float(_selective(model.predict(apply_imputer(imputer, row)), config)[0])
