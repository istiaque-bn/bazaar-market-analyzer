"""Isolated shadow candidates: no production model writes or reads.

Two independent candidates are tracked here, distinguished by
ShadowForecast.candidate_name:
  - "shadow_analogue": the same analogue+ML blend forecast_next_close()
    computes for production (minus bias correction), just never gated
    behind serve_naive_fallback the way production is.
  - "shadow_regression": a direct regression on next-day return, refit
    fresh every cycle from market.services.close_learn's existing
    zero-inflated regression helpers -- the 2026-08-20 target-comparison
    research found this architecture scores skill_vs_naive ~-0.008 (near
    breakeven) on real DSE history, vs. -0.15 to -0.19 for the deployed
    three-class+blend approach on the same data. This shadow candidate
    exists to watch that finding hold up (or not) on genuinely live,
    never-trained-on data for a couple of weeks before it's ever
    considered for promotion -- see docs/NEXT_CLOSE_UPGRADE_TRACKER.md
    Phase 19 (shadow mode) / Phase 20 (staged promotion).
"""
import numpy as np
import pandas as pd
from django.utils import timezone

from market.models import Exchange, ShadowForecast, Stock
from market.services.close_learn import (
    FEATURE_COLS,
    MIN_FOLD_TRAIN_ROWS,
    MIN_HISTORY,
    _build_next_close_panel,
    _feature_row,
    _fit_zero_inflated_next_close,
    _predict_zero_inflated,
    forecast_next_close,
    next_trading_day,
)
from market.services.close_learn_state import liquid_stock_ids
from market.services.indicators import prices_to_df
from market.services.ml_training import apply_imputer, fit_median_imputer

NAME = "shadow_analogue"
REGRESSION_NAME = "shadow_regression"

# Below this many total settled forecasts, a recent-vs-prior split would
# be comparing two tiny, noisy halves — report "not enough history yet"
# instead of a trend that's really just noise.
MIN_TREND_ROWS = 40

# Skill-point swings smaller than this are "holding steady" rather than
# "improving"/"declining" — same rationale and rough magnitude as
# ml_daily_report.COMPARISON_TOLERANCE_PCT (that one compares percentage
# points of precision; this compares points of the 0-1 skill score).
TREND_TOLERANCE = 0.03


def _skill(pred: np.ndarray, actual: np.ndarray, base: np.ndarray) -> float | None:
    mae = float(np.mean(np.abs(pred - actual)))
    base_mae = float(np.mean(np.abs(base - actual)))
    return None if base_mae == 0 else 1 - mae / base_mae


def _train_shadow_regression():
    """Fits fresh every cycle -- deliberately no persisted .pkl and no
    MLModelVersion row. This is a 2+ week isolated watch-and-see
    experiment (see module docstring), not a candidate for the
    promotion/versioning machinery that next_close_rf already has;
    adding that here would be infrastructure this experiment doesn't
    need yet. Returns (imputer, classifier, regressor), or None if
    there isn't enough panel data to fit on."""
    panel = _build_next_close_panel(Exchange.DSE, limit_stocks=80)
    if panel.empty or len(panel) < MIN_FOLD_TRAIN_ROWS:
        return None
    X = panel[FEATURE_COLS].clip(lower=-50, upper=50)
    y = panel["fwd_ret_1"].clip(-0.2, 0.2).to_numpy()
    imputer = fit_median_imputer(X)
    X_i = apply_imputer(imputer, X)
    classifier, regressor = _fit_zero_inflated_next_close(X_i, y)
    return imputer, classifier, regressor


def run_shadow_cycle(as_of=None):
    as_of = as_of or timezone.localdate(); target = next_trading_day(as_of); made=settled=0
    for row in ShadowForecast.objects.filter(actual_close__isnull=True, target_date__lte=as_of).select_related("stock"):
        actual = row.stock.prices.filter(date=row.target_date).values_list("close", flat=True).first()
        if actual is not None: row.actual_close=float(actual); row.settled_at=timezone.now(); row.save(update_fields=["actual_close","settled_at"]); settled+=1
    for stock in Stock.objects.filter(is_active=True):
        df=prices_to_df(stock.prices.live().filter(date__lte=as_of).order_by("date"))
        if len(df)<MIN_HISTORY or ShadowForecast.objects.filter(stock=stock,target_date=target,candidate_name=NAME).exists(): continue
        pred=forecast_next_close(df, return_bias=0.0, exchange=stock.exchange, sector=stock.sector or "")
        if pred: ShadowForecast.objects.create(stock=stock,as_of=as_of,target_date=target,last_close=pred["last_close"],predicted_close=pred["predicted_close"],predicted_return=pred["predicted_return"],candidate_name=NAME); made+=1

    made_regression = 0
    trained = _train_shadow_regression()
    if trained is not None:
        imputer, classifier, regressor = trained
        liquid_stocks = Stock.objects.filter(id__in=liquid_stock_ids(), is_active=True)
        for stock in liquid_stocks:
            if ShadowForecast.objects.filter(stock=stock, target_date=target, candidate_name=REGRESSION_NAME).exists():
                continue
            df = prices_to_df(stock.prices.live().filter(date__lte=as_of).order_by("date"))
            if len(df) < MIN_HISTORY:
                continue
            feats = _feature_row(df, exchange=stock.exchange, sector=stock.sector or "")
            if not feats:
                continue
            last_close = float(df.iloc[-1]["close"])
            if last_close <= 0:
                continue
            row_df = pd.DataFrame([{c: (0.0 if feats.get(c) is None else feats.get(c)) for c in FEATURE_COLS}])
            row_i = apply_imputer(imputer, row_df)
            pred_ret = float(_predict_zero_inflated(classifier, regressor, row_i)[0])
            predicted_close = last_close * (1 + pred_ret)
            ShadowForecast.objects.create(
                stock=stock,
                as_of=as_of,
                target_date=target,
                last_close=round(last_close, 4),
                predicted_close=round(predicted_close, 4),
                predicted_return=round(pred_ret, 6),
                candidate_name=REGRESSION_NAME,
            )
            made_regression += 1

    return {"ok": True, "created": made, "created_regression": made_regression, "settled": settled}


def shadow_report(candidate_name: str = NAME) -> dict:
    """Read-only summary of one shadow candidate's settled forecasts,
    including a recent-vs-prior trend split so callers don't have to
    keep their own day-over-day snapshot (unlike the direction model's
    MlDailyReportDelivery-based comparison — these candidates settle many
    forecasts a day, so "the most recent half vs the rest" is already a
    stable enough trend signal without persisting extra state)."""
    rows = list(ShadowForecast.objects.filter(candidate_name=candidate_name, actual_close__isnull=False).order_by("-target_date")[:10000])
    if not rows:
        return {"n": 0, "trend": None}
    actual = np.array([r.actual_close for r in rows]); pred = np.array([r.predicted_close for r in rows]); base = np.array([r.last_close for r in rows])
    mae = float(np.mean(np.abs(pred - actual))); base_mae = float(np.mean(np.abs(base - actual)))
    skill = _skill(pred, actual, base)
    actual_ret = (actual - base) / base; pred_ret = (pred - base) / base
    directional = actual_ret != 0
    direction = float(np.mean(np.sign(actual_ret[directional]) == np.sign(pred_ret[directional]))) if directional.any() else None

    trend = None
    if len(rows) >= MIN_TREND_ROWS:
        half = len(rows) // 2
        recent, prior = rows[:half], rows[half:]
        recent_skill = _skill(
            np.array([r.predicted_close for r in recent]), np.array([r.actual_close for r in recent]), np.array([r.last_close for r in recent])
        )
        prior_skill = _skill(
            np.array([r.predicted_close for r in prior]), np.array([r.actual_close for r in prior]), np.array([r.last_close for r in prior])
        )
        if recent_skill is not None and prior_skill is not None:
            diff = recent_skill - prior_skill
            trend = "stable" if abs(diff) < TREND_TOLERANCE else ("improving" if diff > 0 else "declining")

    return {
        "n": len(rows),
        "mae": round(mae, 4),
        "naive_mae": round(base_mae, 4),
        "skill": None if skill is None else round(float(skill), 4),
        "direction": None if direction is None else round(direction, 4),
        "trend": trend,
    }


_TREND_LABELS = {
    "improving": "📈 Trend: Improving — it's doing better than it was before.",
    "declining": "📉 Trend: Declining — it's doing worse than it was before.",
    "stable": "➡️ Trend: Holding steady — about the same as before.",
    None: "🆕 Trend: Not enough history yet to tell.",
}

CANDIDATE_LABELS = {
    NAME: "Analogue + ML blend",
    REGRESSION_NAME: "Direct regression (research candidate)",
}

SHADOW_DISCLAIMER = "This is a background experiment only: it is isolated and never changes real forecasts, trades, or anything you see on Bazaar."


def _render_shadow_body(r: dict) -> list[str]:
    """Verdict + explanation lines only — no header, no disclaimer — so
    the single-candidate and two-candidate report builders share
    identical wording per candidate instead of two copies drifting
    apart over time."""
    if r["n"] == 0:
        return ["No completed price checks yet — nothing to report."]

    lines = [_TREND_LABELS[r["trend"]], ""]

    skill = r.get("skill")
    if skill is None:
        lines.append("How good are its guesses? Not enough evidence yet to say.")
    elif skill > 0:
        pct = round(skill * 100)
        lines.append(f"How good are its guesses? On average, its price guesses were about {pct}% closer to the real price than simply assuming \"no change\" tomorrow.")
    else:
        pct = round(abs(skill) * 100)
        lines.append(f"How good are its guesses? On average, its price guesses were about {pct}% further from the real price than simply assuming \"no change\" tomorrow — it is currently doing worse than doing nothing.")

    direction = r.get("direction")
    if direction is not None:
        lines.append(f"When the price actually moved, it guessed the correct direction (up or down) about {round(direction * 100)} times out of 100.")

    lines.append(f"Based on {r['n']} completed price checks so far.")
    return lines


def render_shadow_report_text(r: dict) -> str:
    """Plain-language Telegram text for a single shadow candidate —
    mirrors market.services.ml_daily_report's style (a verdict a
    non-technical reader can act on, not a bare row of numbers)."""
    lines = ["\U0001f9ea Shadow Model Check (test only)", *_render_shadow_body(r), "", SHADOW_DISCLAIMER]
    return "\n".join(lines)


def render_shadow_comparison_text(reports: dict[str, dict]) -> str:
    """Two-candidate Telegram text — the analogue+ML blend and the
    direct-regression research candidate side by side, one shared
    header/disclaimer. ``reports`` maps candidate_name -> shadow_report()
    output, e.g. {NAME: shadow_report(NAME), REGRESSION_NAME:
    shadow_report(REGRESSION_NAME)}."""
    lines = [
        "\U0001f9ea Shadow Model Check (test only)",
        "Comparing two isolated research candidates — neither affects real forecasts.",
    ]
    for name, r in reports.items():
        lines.append("")
        lines.append(f"— {CANDIDATE_LABELS.get(name, name)} —")
        lines.extend(_render_shadow_body(r))
    lines.append("")
    lines.append(SHADOW_DISCLAIMER)
    return "\n".join(lines)
