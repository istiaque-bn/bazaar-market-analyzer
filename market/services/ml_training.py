"""
Shared chronological train/evaluate machinery for Bazaar's two RF models
(market.services.ml_model's forward-return classifier and
market.services.close_learn's next-close regressor).

Design constraints (Phase 4):
  - No random shuffling. Every split is date-ordered walk-forward with an
    embargo gap sized to the forecast horizon, so a training row's label
    (which looks `horizon` trading days into the future) can never overlap
    the date range of the test fold that follows it.
  - Preprocessing (currently: median imputation for stray NaNs) is fit on
    the train fold only and applied unchanged to the test fold.
  - Every trained artifact is versioned (timestamped), backed up before
    being overwritten, and recorded in market.models.MLModelVersion with
    fold-level metadata so results can be audited later.
  - A model is only eligible for `is_active` deployment if its aggregated
    walk-forward skill vs. the majority-class / zero-change baseline is
    strictly positive. Callers (ml_probability / _ml_one_day_return) must
    check this before letting a model influence output.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    brier_score_loss,
    precision_score,
    recall_score,
)

BACKUP_SUBDIR = "backups"


def validate_chronological(df: pd.DataFrame, date_col: str = "date", label: str = "") -> pd.DataFrame:
    """Sort ascending by date_col and reject null/duplicate dates. Raises
    ValueError rather than silently guessing at intent — a duplicate or
    null date means upstream data is broken and must not silently feed a
    forward-looking label."""
    if df.empty:
        return df
    if df[date_col].isna().any():
        raise ValueError(f"{label}: null date(s) in price history — cannot build chronological features")
    out = df.sort_values(date_col, kind="mergesort").reset_index(drop=True)
    if out[date_col].duplicated().any():
        raise ValueError(f"{label}: duplicate date(s) in price history — cannot build chronological features")
    return out


@dataclass
class Fold:
    fold: int
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp


def walk_forward_folds(
    dates: pd.Series,
    n_folds: int = 5,
    embargo_days: int = 14,
    min_train_frac: float = 0.4,
) -> list[Fold]:
    """Expanding-window walk-forward folds over the unique sorted dates of
    a panel. Train = every row with date <= train_end. Test = rows with
    test_start <= date <= test_end. `embargo_days` calendar days separate
    train_end from test_start so no training label's forward-looking
    window can reach into the test period."""
    uniq = np.sort(pd.to_datetime(dates).unique())
    if len(uniq) < 20:
        return []

    first_test_idx = max(int(len(uniq) * min_train_frac), 1)
    if first_test_idx >= len(uniq) - 1:
        return []
    edges = np.linspace(first_test_idx, len(uniq) - 1, num=n_folds + 1, dtype=int)
    edges = sorted(set(edges.tolist()))

    folds: list[Fold] = []
    for k in range(len(edges) - 1):
        test_start_idx = edges[k]
        test_end_idx = edges[k + 1]
        if test_start_idx >= test_end_idx:
            continue
        test_start = pd.Timestamp(uniq[test_start_idx])
        test_end = pd.Timestamp(uniq[test_end_idx])
        train_end = test_start - pd.Timedelta(days=embargo_days)
        folds.append(Fold(fold=len(folds) + 1, train_end=train_end, test_start=test_start, test_end=test_end))
    return folds


def fit_median_imputer(X_train: pd.DataFrame) -> SimpleImputer:
    """Preprocessing must only ever see the training fold."""
    imputer = SimpleImputer(strategy="median")
    imputer.fit(X_train)
    return imputer


def apply_imputer(imputer: SimpleImputer, X: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(imputer.transform(X), columns=X.columns, index=X.index)


RECENCY_HALF_LIFE_DAYS = 180


def recency_weights(dates: pd.Series, half_life_days: int = RECENCY_HALF_LIFE_DAYS, reference: "pd.Timestamp | None" = None) -> np.ndarray:
    """Exponential recency weighting for sample_weight= on .fit(). A row
    `half_life_days` older than `reference` (default: the series' own max,
    i.e. the most recent row in whatever fold/window is being fit) gets
    half the weight of the most recent row. `reference` must be passed
    explicitly inside walk-forward folds — using the fold's train_end
    rather than each call's local max keeps weighting consistent with the
    point in time being simulated, not silently drifting with fold size."""
    dates = pd.to_datetime(pd.Series(dates))
    ref = pd.Timestamp(reference) if reference is not None else dates.max()
    age_days = (ref - dates).dt.days.clip(lower=0)
    return np.power(0.5, age_days / float(half_life_days)).to_numpy()


# ---------------------------------------------------------------------------
# Classification baselines + metrics (used by ml_model.py's 10d-forward task)
# ---------------------------------------------------------------------------


def majority_class_baseline(y_train: np.ndarray, n_test: int) -> tuple[np.ndarray, np.ndarray]:
    if len(y_train) == 0:
        return np.zeros(n_test, dtype=int), np.full(n_test, 0.5)
    majority = int(pd.Series(y_train).mode().iloc[0])
    prob = float(np.mean(y_train))
    return np.full(n_test, majority), np.full(n_test, prob)


def zero_return_baseline(n_test: int) -> tuple[np.ndarray, np.ndarray]:
    """Predicted forward return is exactly 0 -> never counts as ">0" -> always class 0."""
    return np.zeros(n_test, dtype=int), np.zeros(n_test, dtype=float)


def persistence_baseline(trend_train: pd.Series, trend_test: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """Predicts the label continues whatever direction the stock's own
    trailing momentum feature (e.g. return_20d) already shows, as of each
    test row — a same-direction-continues baseline, distinct from the
    train-fold-constant baselines above."""
    pred = (trend_test.fillna(0.0) > 0).astype(int).to_numpy()
    prob = np.clip(0.5 + np.sign(trend_test.fillna(0.0).to_numpy()) * 0.25, 0.0, 1.0)
    return pred, prob


def simple_market_baseline(y_train: np.ndarray, exchange_train: pd.Series, exchange_test: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """Predicts the majority label observed *per exchange* in the training
    fold — a market-wide (not stock-specific) constant."""
    pred = np.zeros(len(exchange_test), dtype=int)
    prob = np.full(len(exchange_test), 0.5)
    for ex in exchange_test.unique():
        mask_train = exchange_train == ex
        mask_test = (exchange_test == ex).to_numpy()
        if mask_train.sum() == 0:
            continue
        p = float(np.mean(y_train[mask_train.to_numpy()]))
        pred[mask_test] = int(round(p))
        prob[mask_test] = p
    return pred, prob


def classification_metrics(y_true: np.ndarray, y_pred: np.ndarray, y_prob: np.ndarray | None) -> dict:
    y_true = np.asarray(y_true)
    y_pred = np.asarray(y_pred)
    out = {
        "n": int(len(y_true)),
        "accuracy": round(float(accuracy_score(y_true, y_pred)), 4),
        "balanced_accuracy": round(float(balanced_accuracy_score(y_true, y_pred)), 4) if len(set(y_true)) > 1 else None,
        "precision": round(float(precision_score(y_true, y_pred, zero_division=0)), 4),
        "recall": round(float(recall_score(y_true, y_pred, zero_division=0)), 4),
        "direction_hit_rate": round(float(accuracy_score(y_true, y_pred)), 4),
        "positive_rate_true": round(float(np.mean(y_true)), 4) if len(y_true) else None,
        "positive_rate_pred": round(float(np.mean(y_pred)), 4) if len(y_pred) else None,
    }
    if y_prob is not None:
        try:
            out["brier"] = round(float(brier_score_loss(y_true, y_prob)), 4)
        except ValueError:
            out["brier"] = None
    else:
        out["brier"] = None
    return out


def skill_vs_baseline(model_metrics: dict, baseline_metrics: dict, key: str = "balanced_accuracy") -> float | None:
    m = model_metrics.get(key)
    b = baseline_metrics.get(key)
    if m is None or b is None:
        return None
    return round(float(m - b), 4)


def brier_skill_score(model_metrics: dict, baseline_metrics: dict) -> float | None:
    m = model_metrics.get("brier")
    b = baseline_metrics.get("brier")
    if m is None or b is None or b <= 1e-12:
        return None
    return round(float(1.0 - m / b), 4)


# ---------------------------------------------------------------------------
# Regression baselines + metrics (used by close_learn.py's next-close task)
# ---------------------------------------------------------------------------


def regression_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict:
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    abs_err = np.abs(y_pred - y_true)
    mae = float(np.mean(abs_err)) if len(abs_err) else None
    nonzero = np.abs(y_true) > 1e-9
    mape = float(np.mean(abs_err[nonzero] / np.abs(y_true[nonzero])) * 100) if nonzero.any() else None
    hit = float(np.mean((y_pred >= 0) == (y_true >= 0))) if len(y_true) else None
    return {
        "n": int(len(y_true)),
        "mae": round(mae, 6) if mae is not None else None,
        "mape": round(mape, 4) if mape is not None else None,
        "direction_hit_rate": round(hit, 4) if hit is not None else None,
    }


def regression_skill(model_metrics: dict, baseline_metrics: dict) -> float | None:
    m = model_metrics.get("mae")
    b = baseline_metrics.get("mae")
    if m is None or b is None or b <= 1e-12:
        return None
    return round(float(1.0 - m / b), 4)


# ---------------------------------------------------------------------------
# Versioning + backups
# ---------------------------------------------------------------------------


def new_version_tag() -> str:
    from django.utils import timezone

    return timezone.now().strftime("%Y%m%d-%H%M%S")


def _backup_tag() -> str:
    from django.utils import timezone

    # Microsecond precision so a backup's tag never collides with the new
    # model's own version tag (new_version_tag()), which is generated by a
    # separate call a moment later in _final_fit_and_save — same-second
    # collisions would otherwise make a backup look like it IS the new
    # version instead of the one it's superseding.
    return timezone.now().strftime("%Y%m%d-%H%M%S-%f")


def backup_existing_model(model_path: Path) -> str | None:
    """Copy (never move/delete) the current deployed artifact aside before
    it is overwritten, so a bad retrain can always be rolled back by hand."""
    if not model_path.exists():
        return None
    backup_dir = model_path.parent / BACKUP_SUBDIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    tag = _backup_tag()
    dest = backup_dir / f"{model_path.stem}__{tag}{model_path.suffix}"
    shutil.copy2(model_path, dest)
    return str(dest)


def record_model_version(
    *,
    model_name: str,
    version: str,
    exchange_scope: str,
    status: str,
    is_active: bool,
    data_cutoff: date,
    feature_schema: list[str],
    train_rows: int,
    fold_metadata: list[dict],
    metrics: dict,
    file_path: str,
    backup_path: str | None,
    notes: str = "",
):
    from market.models import MLModelVersion

    if is_active:
        MLModelVersion.objects.filter(model_name=model_name, exchange_scope=exchange_scope, is_active=True).update(
            is_active=False
        )
    return MLModelVersion.objects.create(
        model_name=model_name,
        version=version,
        exchange_scope=exchange_scope,
        status=status,
        is_active=is_active,
        data_cutoff=data_cutoff,
        feature_schema=feature_schema,
        train_rows=train_rows,
        fold_metadata=fold_metadata,
        metrics=metrics,
        file_path=file_path,
        backup_path=backup_path or "",
        notes=notes,
    )


def active_model_version(model_name: str, exchange_scope: str = "combined"):
    from market.models import MLModelVersion

    return (
        MLModelVersion.objects.filter(model_name=model_name, exchange_scope=exchange_scope, is_active=True)
        .order_by("-trained_at")
        .first()
    )
