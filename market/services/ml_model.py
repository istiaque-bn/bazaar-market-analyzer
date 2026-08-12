"""
Forward 10-trading-day-return classifier — probability that a stock's
close 10 sessions from now is higher than today's, blended into the rule
score to nudge (not replace) predictor.py's output.

Phase 4 correctness rules (see market/services/ml_training.py for the
shared machinery these build on):
  - A row whose 10-day-forward outcome is not yet known (i.e. it's within
    the last 10 trading days of a stock's history) gets label = NaN, not
    label = 0. It is dropped, never treated as "no gain".
  - Evaluation is chronological walk-forward with an embargo gap, never a
    random train_test_split — a training row's label looks 10 sessions
    ahead, so any training row within that horizon of a test window's
    start would leak information about the test period.
  - DSE and CSE are evaluated (and, by default, deployed) separately.
    They are only pooled into one combined model when walk-forward skill
    is roughly consistent between them and both have enough out-of-sample
    rows to trust the comparison — see _justify_combining().
  - A trained artifact is only marked active (and therefore usable by
    ml_probability) if its walk-forward skill vs. the majority-class
    baseline is strictly positive. Otherwise it's saved as "experimental"
    for audit/inspection but ml_probability() refuses to serve it, so it
    cannot influence is_safe_buy / BUY recommendations.
"""
from __future__ import annotations

import logging
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from django.conf import settings
from xgboost import XGBClassifier

from market.models import Exchange, Stock
from market.services.close_learn import _attach_market_features
from market.services.indicators import compute_indicators, prices_to_df, volatility_regime
from market.services.ml_training import (
    active_model_version,
    apply_imputer,
    backup_existing_model,
    classification_metrics,
    fit_median_imputer,
    majority_class_baseline,
    new_version_tag,
    persistence_baseline,
    record_model_version,
    simple_market_baseline,
    skill_vs_baseline,
    validate_chronological,
    walk_forward_folds,
    zero_return_baseline,
)

logger = logging.getLogger(__name__)

MODEL_NAME = "forward_return_rf"
CACHE_DIR = Path(settings.CACHE_DIR)
MODEL_PATH = CACHE_DIR / "forward_return_rf.pkl"  # combined-scope deployment
MODEL_PATH_BY_EXCHANGE = {
    Exchange.DSE: CACHE_DIR / "forward_return_rf_DSE.pkl",
    Exchange.CSE: CACHE_DIR / "forward_return_rf_CSE.pkl",
}
STATUS_ACTIVE = "active"
STATUS_EXPERIMENTAL = "experimental"

FORWARD_HORIZON_TRADING_DAYS = 10
EMBARGO_CALENDAR_DAYS = 14  # >= 10 Sun-Thu trading days, so no train label window reaches into a test fold
MIN_STOCK_ROWS = 80
MIN_PANEL_ROWS = 200
MIN_FOLD_TRAIN_ROWS = 100
MIN_FOLD_TEST_ROWS = 20
MIN_ROWS_PER_EXCHANGE_TO_POOL = 200

FEATURE_COLS = [
    "rsi_14",
    "macd_hist",
    "return_5d",
    "return_20d",
    "volatility_20",
    "volume_ratio",
    "turnover_ratio",
    "dist_sma20",
    "dist_sma50",
    "rel_ret_1d",
    "rel_ret_5d",
    "rel_ret_20d",
    "stock_sector_rel_1d",
    "sector_index_rel_1d",
    "vol_regime",
    "trend_regime",
]


def _feature_frame(df: pd.DataFrame, exchange: str = "DSE", sector: str = "") -> pd.DataFrame:
    data = compute_indicators(df)
    if data.empty:
        return pd.DataFrame()
    out = data.copy()
    out["volume_ratio"] = out["volume"] / out["volume_sma_20"].replace(0, np.nan)
    # Traded value (price x volume), not just share count — comparable
    # across stocks that span a huge price range (5-taka penny names to
    # 500+-taka blue chips), where raw volume alone isn't apples-to-apples.
    # Unlike the other features here, a missing/zero "value" (legacy rows
    # predating turnover tracking, a genuine no-trade day, or a caller
    # that built its DataFrame by hand without a "value" column at all)
    # must NOT turn into NaN: build_training_panel() does a blanket
    # dropna() across all FEATURE_COLS, so an unfillable ratio here would
    # silently drop the whole row — shrinking the training set — rather
    # than just losing this one feature's signal for that row. Fall back
    # to 1.0 ("in line with its own recent average") instead.
    if "value" in out.columns:
        out["turnover_ratio"] = (out["value"] / out["value"].rolling(20, min_periods=10).mean().replace(0, np.nan)).fillna(1.0)
    else:
        out["turnover_ratio"] = 1.0
    out["dist_sma20"] = out["close"] / out["sma_20"] - 1
    out["dist_sma50"] = out["close"] / out["sma_50"] - 1
    out["vol_regime"] = volatility_regime(out["volatility_20"])
    # Reuse close_learn's exchange/sector context machinery for relative-
    # strength + regime features (rel_ret_*, stock_sector_rel_1d,
    # sector_index_rel_1d, trend_regime) rather than duplicating it —
    # same leak-safe, backward-looking construction either model uses.
    out["stock_ret_1d"] = out["return_1d"]
    start = pd.Timestamp(out["date"].iloc[0]).date()
    end = pd.Timestamp(out["date"].iloc[-1]).date()
    out = _attach_market_features(out, exchange, sector, start=start, end=end)
    out["fwd_ret_10"] = out["close"].shift(-10) / out["close"] - 1
    # Rows near the end of a stock's history have no known 10-day-forward
    # outcome yet. Those must be dropped, not silently scored as "flat/
    # negative" (label 0) — leave label NaN so the caller's dropna() drops
    # the row entirely instead of teaching the model a fabricated class.
    out["label"] = np.where(out["fwd_ret_10"].notna(), (out["fwd_ret_10"] > 0).astype(float), np.nan)
    return out


def build_training_panel(exchange: str, limit_stocks: int | None = None) -> pd.DataFrame:
    """One row per (stock, date) with features + label, for a single
    exchange. Each stock's history is chronologically validated (sorted,
    no null/duplicate dates) before any feature is computed. Bars carrying
    a data_quality.TRAINING_EXCLUDE_FLAGS flag (impossible OHLC, abnormal
    single-day jump) are dropped before feature construction."""
    from market.services.data_quality import TRAINING_EXCLUDE_FLAGS

    qs = Stock.objects.filter(is_active=True, exchange=exchange).order_by("id")
    if limit_stocks:
        qs = qs[:limit_stocks]

    frames = []
    for stock in qs:
        df = prices_to_df(stock.prices.live(), exclude_quality_flags=TRAINING_EXCLUDE_FLAGS)
        if len(df) < MIN_STOCK_ROWS:
            continue
        df = validate_chronological(df, label=f"{stock.exchange}:{stock.trading_code}")
        feats = _feature_frame(df, exchange=stock.exchange, sector=stock.sector or "")
        if feats.empty:
            continue
        cols = feats[["date"] + FEATURE_COLS + ["label"]].dropna()
        if cols.empty:
            continue
        cols = cols.copy()
        cols["stock_id"] = stock.id
        cols["exchange"] = stock.exchange
        frames.append(cols)
    if not frames:
        return pd.DataFrame()
    panel = pd.concat(frames, ignore_index=True)
    panel["date"] = pd.to_datetime(panel["date"])
    return panel.sort_values("date", kind="mergesort").reset_index(drop=True)


def _fold_class_balance(y) -> dict:
    y = np.asarray(y)
    if len(y) == 0:
        return {}
    vals, counts = np.unique(y, return_counts=True)
    return {str(int(v)): int(c) for v, c in zip(vals, counts)}


def walk_forward_evaluate(panel: pd.DataFrame, n_folds: int = 5) -> dict:
    """Chronological walk-forward CV with an embargo gap >= the label's
    forward horizon. Reports model + baseline metrics per fold and
    aggregated; does not persist anything."""
    if panel.empty:
        return {"ok": False, "error": "no data"}

    folds = walk_forward_folds(panel["date"], n_folds=n_folds, embargo_days=EMBARGO_CALENDAR_DAYS)
    if not folds:
        return {"ok": False, "error": "not enough distinct trading dates for walk-forward folds"}

    fold_rows = []
    model_metrics_list = []
    baseline_metrics_list: dict[str, list[dict]] = {
        "majority_class": [],
        "zero_return": [],
        "persistence": [],
        "simple_market": [],
    }

    for f in folds:
        train = panel.loc[panel["date"] <= f.train_end]
        test = panel.loc[(panel["date"] >= f.test_start) & (panel["date"] <= f.test_end)]
        if len(train) < MIN_FOLD_TRAIN_ROWS or len(test) < MIN_FOLD_TEST_ROWS or train["label"].nunique() < 2:
            continue

        X_train, y_train = train[FEATURE_COLS], train["label"].to_numpy()
        X_test, y_test = test[FEATURE_COLS], test["label"].to_numpy()

        imputer = fit_median_imputer(X_train)  # preprocessing fit on the train fold only
        X_train_i = apply_imputer(imputer, X_train)
        X_test_i = apply_imputer(imputer, X_test)

        model = XGBClassifier(n_estimators=120, max_depth=4, learning_rate=0.05, subsample=0.8, colsample_bytree=0.8, random_state=42, n_jobs=settings.ML_MAX_WORKERS, eval_metric="logloss")
        model.fit(X_train_i, y_train)
        classes = list(model.classes_)
        proba = model.predict_proba(X_test_i)
        prob1 = proba[:, classes.index(1.0)] if 1.0 in classes else np.zeros(len(X_test_i))
        y_pred = (prob1 >= 0.5).astype(int)

        m_metrics = classification_metrics(y_test, y_pred, prob1)
        model_metrics_list.append(m_metrics)

        maj_pred, maj_prob = majority_class_baseline(y_train, len(y_test))
        zero_pred, zero_prob = zero_return_baseline(len(y_test))
        pers_pred, pers_prob = persistence_baseline(train["return_20d"], test["return_20d"])
        mkt_pred, mkt_prob = simple_market_baseline(y_train, train["exchange"], test["exchange"])

        baseline_metrics_list["majority_class"].append(classification_metrics(y_test, maj_pred, maj_prob))
        baseline_metrics_list["zero_return"].append(classification_metrics(y_test, zero_pred, zero_prob))
        baseline_metrics_list["persistence"].append(classification_metrics(y_test, pers_pred, pers_prob))
        baseline_metrics_list["simple_market"].append(classification_metrics(y_test, mkt_pred, mkt_prob))

        fold_rows.append(
            {
                "fold": f.fold,
                "train_end": f.train_end.date().isoformat(),
                "test_start": f.test_start.date().isoformat(),
                "test_end": f.test_end.date().isoformat(),
                "train_rows": int(len(train)),
                "test_rows": int(len(test)),
                "train_class_balance": _fold_class_balance(y_train),
                "test_class_balance": _fold_class_balance(y_test),
                "metrics": m_metrics,
            }
        )

    if not fold_rows:
        return {"ok": False, "error": "no fold had enough rows/class variety after the embargo gap"}

    def _agg(dicts: list[dict], key: str):
        vals = [d[key] for d in dicts if d.get(key) is not None]
        return round(float(np.mean(vals)), 4) if vals else None

    metric_keys = ("accuracy", "balanced_accuracy", "precision", "recall", "brier", "direction_hit_rate")
    agg_model = {k: _agg(model_metrics_list, k) for k in metric_keys}
    agg_model["n"] = int(sum(m["n"] for m in model_metrics_list))

    agg_baselines = {
        name: {k: _agg(lst, k) for k in metric_keys} for name, lst in baseline_metrics_list.items()
    }

    skill = {name: skill_vs_baseline(agg_model, agg_baselines[name]) for name in agg_baselines}

    return {
        "ok": True,
        "n_folds_used": len(fold_rows),
        "folds": fold_rows,
        "model_metrics": agg_model,
        "baseline_metrics": agg_baselines,
        "skill_vs_baseline": skill,
        # Majority-class is the naive reference used for the deployment gate:
        # a model that can't beat "always predict the more common class" adds
        # nothing.
        "skill_vs_naive": skill.get("majority_class"),
    }


def _justify_combining(dse_eval: dict, cse_eval: dict) -> tuple[bool, str]:
    if not dse_eval.get("ok") or not cse_eval.get("ok"):
        return False, "one exchange had too little data to evaluate separately, so pooling can't be justified against it"
    dse_n, cse_n = dse_eval["model_metrics"]["n"], cse_eval["model_metrics"]["n"]
    if dse_n < MIN_ROWS_PER_EXCHANGE_TO_POOL or cse_n < MIN_ROWS_PER_EXCHANGE_TO_POOL:
        return False, f"insufficient out-of-sample rows to trust pooling (DSE={dse_n}, CSE={cse_n}, need >={MIN_ROWS_PER_EXCHANGE_TO_POOL} each)"
    dse_skill, cse_skill = dse_eval.get("skill_vs_naive"), cse_eval.get("skill_vs_naive")
    if dse_skill is None or cse_skill is None:
        return False, "walk-forward skill undefined for one exchange"
    if abs(dse_skill - cse_skill) > 0.15:
        return False, f"DSE/CSE walk-forward skill diverge too much to justify one pooled model (DSE={dse_skill}, CSE={cse_skill})"
    return True, f"DSE/CSE walk-forward skill are consistent (DSE={dse_skill}, CSE={cse_skill}); pooling adds sample size without hiding a weak exchange"


def _final_fit_and_save(panel: pd.DataFrame, eval_result: dict, *, exchange_scope: str, model_path: Path) -> dict:
    X, y = panel[FEATURE_COLS], panel["label"].to_numpy()
    imputer = fit_median_imputer(X)
    X_i = apply_imputer(imputer, X)
    model = XGBClassifier(n_estimators=120, max_depth=4, learning_rate=0.05, subsample=0.8, colsample_bytree=0.8, random_state=42, n_jobs=settings.ML_MAX_WORKERS, eval_metric="logloss")
    model.fit(X_i, y)

    skill = eval_result.get("skill_vs_naive")
    is_active = skill is not None and skill > 0
    status = STATUS_ACTIVE if is_active else STATUS_EXPERIMENTAL

    backup_path = backup_existing_model(model_path)
    version = new_version_tag()
    data_cutoff = panel["date"].max().date()

    joblib.dump(
        {
            "model": model,
            "imputer": imputer,
            "features": FEATURE_COLS,
            "version": version,
            "exchange_scope": exchange_scope,
            "status": status,
            "walk_forward": eval_result,
            "data_cutoff": data_cutoff.isoformat(),
            "trained_rows": int(len(panel)),
        },
        model_path,
    )

    record_model_version(
        model_name=MODEL_NAME,
        version=version,
        exchange_scope=exchange_scope,
        status=status,
        is_active=is_active,
        data_cutoff=data_cutoff,
        feature_schema=FEATURE_COLS,
        train_rows=int(len(panel)),
        fold_metadata=eval_result.get("folds", []),
        metrics={
            "model": eval_result.get("model_metrics"),
            "baselines": eval_result.get("baseline_metrics"),
            "skill_vs_baseline": eval_result.get("skill_vs_baseline"),
        },
        file_path=str(model_path),
        backup_path=backup_path,
        notes="" if is_active else "Non-positive out-of-sample skill vs majority-class baseline; not deployed for confident output.",
    )

    return {
        "ok": True,
        "status": status,
        "is_active": is_active,
        "skill_vs_naive": skill,
        "version": version,
        "train_rows": int(len(panel)),
        "path": str(model_path),
        "backup_path": backup_path,
    }


def train_model(limit_stocks: int = 120, force: bool = False) -> dict:
    """Evaluate + (re)train the forward-return classifier. Always
    evaluates DSE and CSE separately via chronological walk-forward CV;
    only pools them into one deployed model when _justify_combining()
    says the comparison supports it (see module docstring).

    Skips the (expensive) walk-forward + fit if no label-resolvable data
    newer than what's already deployed has arrived — every manual
    "Analyze"/"Fetch" click and run_full_analysis(train_ml=True) call used
    to force a full retrain unconditionally, several times a day,
    producing byte-identical metrics since nothing had actually changed.
    Compared against the panel's own max date rather than the latest raw
    price date: a 10-day-forward label means the newest ~10 trading days
    of prices can never resolve to a usable row, so data_cutoff
    structurally lags raw price data by that much and would never match
    it. Pass force=True to retrain regardless, e.g. after a feature/code
    change."""
    from market.services.exchange_config import enabled_exchanges

    enabled = enabled_exchanges()
    # A disabled exchange never enters training at all — not just "not
    # deployed" but never evaluated, never pooled into a combined panel.
    # An empty DataFrame here flows through the existing insufficient-rows
    # path below exactly like "not enough real data yet" would, so no
    # further special-casing is needed downstream.
    dse_panel = build_training_panel(Exchange.DSE, limit_stocks=limit_stocks) if Exchange.DSE in enabled else pd.DataFrame()
    cse_panel = build_training_panel(Exchange.CSE, limit_stocks=limit_stocks) if Exchange.CSE in enabled else pd.DataFrame()

    if dse_panel.empty and cse_panel.empty:
        return {"ok": False, "error": "Not enough data to train"}

    if not force:
        candidate_cutoff = max(p["date"].max().date() for p in (dse_panel, cse_panel) if not p.empty)
        already_current = any(
            (v := active_model_version(MODEL_NAME, exchange_scope=scope)) is not None
            and v.data_cutoff is not None
            and v.data_cutoff >= candidate_cutoff
            for scope in ("combined", Exchange.DSE, Exchange.CSE)
        )
        if already_current:
            return {"ok": True, "skipped": "no new label-resolvable data since last train", "data_cutoff": candidate_cutoff.isoformat()}

    dse_eval = walk_forward_evaluate(dse_panel) if len(dse_panel) >= MIN_PANEL_ROWS else {"ok": False, "error": f"insufficient DSE rows ({len(dse_panel)})"}
    cse_eval = walk_forward_evaluate(cse_panel) if len(cse_panel) >= MIN_PANEL_ROWS else {"ok": False, "error": f"insufficient CSE rows ({len(cse_panel)})"}

    panel_all = pd.concat([dse_panel, cse_panel], ignore_index=True) if not (dse_panel.empty or cse_panel.empty) else (dse_panel if not dse_panel.empty else cse_panel)
    panel_all = panel_all.sort_values("date", kind="mergesort").reset_index(drop=True)
    combined_eval = walk_forward_evaluate(panel_all) if len(panel_all) >= MIN_PANEL_ROWS else {"ok": False, "error": f"insufficient combined rows ({len(panel_all)})"}

    justified, reason = _justify_combining(dse_eval, cse_eval)
    evaluation = {"dse": dse_eval, "cse": cse_eval, "combined": combined_eval, "combine_justified": justified, "combine_reason": reason}

    deployed = {}
    if justified and combined_eval.get("ok"):
        deployed["combined"] = _final_fit_and_save(panel_all, combined_eval, exchange_scope="combined", model_path=MODEL_PATH)
    else:
        if dse_eval.get("ok") and len(dse_panel) >= MIN_PANEL_ROWS:
            deployed["DSE"] = _final_fit_and_save(dse_panel, dse_eval, exchange_scope=Exchange.DSE, model_path=MODEL_PATH_BY_EXCHANGE[Exchange.DSE])
        if cse_eval.get("ok") and len(cse_panel) >= MIN_PANEL_ROWS:
            deployed["CSE"] = _final_fit_and_save(cse_panel, cse_eval, exchange_scope=Exchange.CSE, model_path=MODEL_PATH_BY_EXCHANGE[Exchange.CSE])
        if not deployed:
            return {"ok": False, "error": "neither exchange had enough data to evaluate/train separately", "evaluation": evaluation}

    return {"ok": True, "evaluation": evaluation, "deployed": deployed}


def _load_bundle(model_path: Path) -> dict | None:
    if not model_path.exists():
        return None
    try:
        return joblib.load(model_path)
    except Exception as exc:
        logger.warning("Failed to load ML model %s: %s", model_path, exc)
        return None


def _is_deployable(bundle: dict, exchange_scope: str) -> bool:
    """The pickle's own "status" is a snapshot from train time; the
    MLModelVersion row is the live source of truth (a model can be
    downgraded post-hoc, e.g. by live skill monitoring, without a
    retrain). Both must agree the exact trained version is active."""
    if bundle.get("status") != STATUS_ACTIVE:
        return False
    active = active_model_version(MODEL_NAME, exchange_scope=exchange_scope)
    return active is not None and active.version == bundle.get("version")


def load_model(exchange: str | None = None) -> dict | None:
    """Prefer a combined-scope model if it's active; otherwise fall back
    to a per-exchange model if that one is active. A bundle with an
    unrecognized/missing status (e.g. a stale pre-Phase-4 artifact) is
    treated as not-active — fail closed rather than silently serving an
    unverified model.

    A "combined" bundle is skipped entirely (not deleted, not deactivated
    in the DB — just bypassed for serving) whenever any exchange is
    currently disabled: it was necessarily trained on pooled DSE+CSE data
    (see train_model's _justify_combining), and that provenance conflicts
    with a DSE-only deployment's guarantee that CSE data never influences
    what gets served. The next successful train_model() run naturally
    produces a fresh DSE-scoped artifact instead (CSE panels build empty
    while disabled, so _justify_combining can never re-justify pooling) —
    this bypass just covers the gap between disabling CSE and that next
    scheduled retrain."""
    from market.services.exchange_config import enabled_exchanges

    from market.models import Exchange

    combined_is_safe_to_serve = set(enabled_exchanges()) >= {Exchange.DSE, Exchange.CSE}
    combined = _load_bundle(MODEL_PATH) if combined_is_safe_to_serve else None
    if combined is not None and _is_deployable(combined, "combined"):
        return combined
    if exchange and exchange in MODEL_PATH_BY_EXCHANGE:
        per_exchange = _load_bundle(MODEL_PATH_BY_EXCHANGE[exchange])
        if per_exchange is not None and _is_deployable(per_exchange, exchange):
            return per_exchange
    return None


def ml_probability(df: pd.DataFrame, exchange: str | None = None, sector: str = "") -> float | None:
    bundle = load_model(exchange=exchange)
    if bundle is None:
        return None
    feats = _feature_frame(df, exchange=exchange or "DSE", sector=sector)
    if feats.empty:
        return None
    # Use the feature list the *deployed bundle* was actually trained
    # with, not the live FEATURE_COLS constant — those two can legitimately
    # diverge (e.g. mid-experiment on a new feature) without the active
    # model having been retrained yet, and inference must keep matching
    # whatever artifact is actually loaded, not today's module state.
    feature_cols = bundle.get("features", FEATURE_COLS)
    if any(c not in feats.columns for c in feature_cols):
        return None
    row = feats.iloc[[-1]][feature_cols]
    if row.isna().any(axis=None):
        return None
    imputer = bundle.get("imputer")
    if imputer is not None:
        row = apply_imputer(imputer, row)
    model = bundle["model"]
    proba = model.predict_proba(row)[0]
    classes = list(model.classes_)
    if 1.0 not in classes:
        return None
    return float(proba[classes.index(1.0)])


def blend_score(rule_score: float, ml_prob: float | None) -> float:
    """Blend rule score (-100..100) with ML probability (0..1)."""
    if ml_prob is None:
        return rule_score
    ml_score = (ml_prob - 0.5) * 100  # map 0.5->0, 1->50, 0->-50
    return float(np.clip(0.7 * rule_score + 0.3 * ml_score * 2, -100, 100))
