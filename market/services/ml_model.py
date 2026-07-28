"""
Phase 5 — lightweight ML refinement.

Trains a RandomForest on engineered features from ~1y history to estimate
probability that forward 10-day return is positive. Blends with rule score.
"""
from __future__ import annotations

import logging
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from django.conf import settings
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split

from market.models import Stock
from market.services.indicators import compute_indicators, prices_to_df

logger = logging.getLogger(__name__)

MODEL_PATH = Path(settings.CACHE_DIR) / "forward_return_rf.pkl"
FEATURE_COLS = [
    "rsi_14",
    "macd_hist",
    "return_5d",
    "return_20d",
    "volatility_20",
    "volume_ratio",
    "dist_sma20",
    "dist_sma50",
]


def _feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    data = compute_indicators(df)
    if data.empty:
        return pd.DataFrame()
    out = data.copy()
    out["volume_ratio"] = out["volume"] / out["volume_sma_20"].replace(0, np.nan)
    out["dist_sma20"] = out["close"] / out["sma_20"] - 1
    out["dist_sma50"] = out["close"] / out["sma_50"] - 1
    out["fwd_ret_10"] = out["close"].shift(-10) / out["close"] - 1
    out["label"] = (out["fwd_ret_10"] > 0).astype(int)
    return out


def build_training_matrix(limit_stocks: int = 40) -> tuple[pd.DataFrame, pd.Series] | tuple[None, None]:
    frames = []
    for stock in Stock.objects.filter(is_active=True)[:limit_stocks]:
        df = prices_to_df(stock.prices.all())
        if len(df) < 80:
            continue
        feats = _feature_frame(df)
        if feats.empty:
            continue
        frames.append(feats[FEATURE_COLS + ["label"]].dropna())
    if not frames:
        return None, None
    data = pd.concat(frames, ignore_index=True)
    if len(data) < 200:
        return None, None
    return data[FEATURE_COLS], data["label"]


def train_model() -> dict:
    X, y = build_training_matrix()
    if X is None:
        return {"ok": False, "error": "Not enough data to train"}
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.25, random_state=42, stratify=y)
    model = RandomForestClassifier(
        n_estimators=120,
        max_depth=6,
        min_samples_leaf=8,
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X_train, y_train)
    acc = float(model.score(X_test, y_test))
    joblib.dump({"model": model, "features": FEATURE_COLS, "accuracy": acc}, MODEL_PATH)
    return {"ok": True, "accuracy": round(acc, 4), "train_rows": len(X_train), "test_rows": len(X_test), "path": str(MODEL_PATH)}


def load_model():
    if not MODEL_PATH.exists():
        return None
    try:
        return joblib.load(MODEL_PATH)
    except Exception as exc:
        logger.warning("Failed to load ML model: %s", exc)
        return None


def ml_probability(df: pd.DataFrame) -> float | None:
    bundle = load_model()
    if bundle is None:
        return None
    feats = _feature_frame(df)
    if feats.empty:
        return None
    row = feats.iloc[[-1]][FEATURE_COLS]
    if row.isna().any(axis=None):
        return None
    model = bundle["model"]
    proba = model.predict_proba(row)[0]
    # Probability of class 1 (positive forward return)
    classes = list(model.classes_)
    if 1 not in classes:
        return None
    return float(proba[classes.index(1)])


def blend_score(rule_score: float, ml_prob: float | None) -> float:
    """Blend rule score (-100..100) with ML probability (0..1)."""
    if ml_prob is None:
        return rule_score
    ml_score = (ml_prob - 0.5) * 100  # map 0.5->0, 1->50, 0->-50
    return float(np.clip(0.7 * rule_score + 0.3 * ml_score * 2, -100, 100))
