"""Isolated shadow candidate: no production model writes or reads."""
from django.utils import timezone
from market.models import ShadowForecast, Stock
from market.services.close_learn import MIN_HISTORY, forecast_next_close, next_trading_day
from market.services.indicators import prices_to_df
import numpy as np

NAME = "shadow_analogue"

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
    return {"ok":True,"created":made,"settled":settled}


def shadow_report() -> dict:
    """Read-only summary of the shadow candidate's settled forecasts,
    including a recent-vs-prior trend split so callers don't have to
    keep their own day-over-day snapshot (unlike the direction model's
    MlDailyReportDelivery-based comparison — this candidate settles many
    forecasts a day, so "the most recent half vs the rest" is already a
    stable enough trend signal without persisting extra state)."""
    rows = list(ShadowForecast.objects.filter(candidate_name=NAME, actual_close__isnull=False).order_by("-target_date")[:10000])
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


def render_shadow_report_text(r: dict) -> str:
    """Plain-language Telegram text for the shadow candidate — mirrors
    market.services.ml_daily_report's style (a verdict a non-technical
    reader can act on, not a bare row of numbers) rather than the
    previous one-line dump of raw MAE/skill/direction figures."""
    lines = ["\U0001f9ea Shadow Model Check (test only)"]

    if r["n"] == 0:
        lines.append("No completed price checks yet — nothing to report.")
        lines.append("This is a background experiment: it never affects real forecasts, trades, or anything you see on Bazaar.")
        return "\n".join(lines)

    lines.append(_TREND_LABELS[r["trend"]])
    lines.append("")

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
    lines.append("")
    lines.append("This is a background experiment only: it is isolated and never changes real forecasts, trades, or anything you see on Bazaar.")
    return "\n".join(lines)
