"""
Phase 7: consolidated, honest status for forecast/signal presentation.

Bazaar's rule-based score (predictor.py) always produces an action/score —
it is a heuristic, not something that is "trained" or can be under-sampled.
What *can* fail to show a demonstrated edge is the machine-learned layer
that sits on top of it:

  - the forward-return classifier (market.services.ml_model), gated at
    train time to `is_active` only when its walk-forward skill vs. a
    majority-class baseline is strictly positive;
  - the next-day close learner (market.services.close_learn), scored
    on-line against a naive baseline (tomorrow's close = today's close)
    as forecasts settle.

`signal_status()` reads both of those (never guesses), decides whether
either currently demonstrates a positive, adequately-sampled edge, and
returns plain-language material (freshness, liquidity, invalidation,
signal composition) for the web UI, the API, and notifications to present
consistently. Nothing here writes to the database.
"""
from __future__ import annotations

from django.utils import timezone

from market.models import AnalysisResult, MLModelVersion, Stock

ML_MODEL_NAME = "forward_return_rf"

# A walk-forward skill figure computed on very few rows is unreliable
# regardless of its sign — this mirrors ml_model.MIN_PANEL_ROWS, the
# minimum panel size trained against at all.
MIN_ML_TRAIN_ROWS = 200

# Fewer settled next-close forecasts than this can't support a skill claim
# either way; treat it as "not enough evidence yet", not "no skill".
MIN_CLOSE_LEARN_SETTLED = 30

# How far back close_learn_edge_status looks. Bounded (rather than the full
# ledger) so a serving/bias fix shows up in days, not the weeks it'd take
# for new forecasts to outnumber old ones in an unbounded window — see
# compute_skill_metrics's `since` docstring.
CLOSE_LEARN_SKILL_WINDOW_DAYS = 14

# DSE/CSE trade Sun-Thu; a normal weekend gap is up to 3 calendar days.
# +1 day of buffer for a holiday before calling data stale.
STALE_DATA_DAYS = 4

BASELINE_LABELS = {
    "majority_class": "always predicting the more common class",
    "zero_return": "predicting no price change",
    "persistence": "repeating the last 20-day trend",
    "simple_market": "predicting the exchange's average outcome",
}


def ml_model_status(exchange: str) -> dict:
    """Deployment status of the forward-return classifier as it actually
    applies to this exchange: prefer a combined-scope model if active,
    otherwise a per-exchange one — the same precedence
    ml_model.load_model() uses at inference time, so this describes
    exactly what (if anything) the score panel used."""
    combined = (
        MLModelVersion.objects.filter(model_name=ML_MODEL_NAME, exchange_scope="combined").order_by("-trained_at").first()
    )
    per_exchange = (
        MLModelVersion.objects.filter(model_name=ML_MODEL_NAME, exchange_scope=exchange).order_by("-trained_at").first()
    )
    if combined and combined.is_active:
        version = combined
    elif per_exchange and per_exchange.is_active:
        version = per_exchange
    else:
        # Neither is deployable; still report the most recent attempt (if
        # any) so the status page can explain *why* nothing is deployed
        # rather than just saying "none".
        candidates = [v for v in (combined, per_exchange) if v is not None]
        version = max(candidates, key=lambda v: v.trained_at) if candidates else None

    if version is None:
        return {
            "status": "none",
            "deployed": False,
            "exchange_scope": None,
            "version": None,
            "data_cutoff": None,
            "train_rows": 0,
            "trained_at": None,
            "skill_vs_naive": None,
            "direction_hit_rate": None,
            "baseline": BASELINE_LABELS["majority_class"],
            "last_evaluation_period": None,
            "has_edge": False,
        }

    metrics = version.metrics or {}
    skill = (metrics.get("skill_vs_baseline") or {}).get("majority_class")
    model_metrics = metrics.get("model") or {}
    folds = version.fold_metadata or []
    last_fold = folds[-1] if folds else None
    has_edge = bool(version.is_active) and skill is not None and skill > 0 and version.train_rows >= MIN_ML_TRAIN_ROWS
    return {
        "status": version.status,
        "deployed": bool(version.is_active),
        "exchange_scope": version.exchange_scope,
        "version": version.version,
        "data_cutoff": version.data_cutoff.isoformat() if version.data_cutoff else None,
        "train_rows": version.train_rows,
        "trained_at": version.trained_at.isoformat() if version.trained_at else None,
        "skill_vs_naive": skill,
        "direction_hit_rate": model_metrics.get("direction_hit_rate"),
        "baseline": BASELINE_LABELS["majority_class"],
        "last_evaluation_period": (
            {"start": last_fold["test_start"], "end": last_fold["test_end"]} if last_fold else None
        ),
        "has_edge": has_edge,
    }


def close_learn_edge_status() -> dict:
    """Fresh live skill for the served next-close forecasts.

    ``NextDayCloseForecast`` is the authoritative settlement ledger for
    this learner.  A reliability assessment is useful for the release
    decision, but is an immutable historical snapshot; using it for an
    operational alert can leave the alert reporting an old sample count
    and skill after new forecasts settle.  Recompute from the ledger so
    the operations page and alert always agree on the current evidence.
    """
    from datetime import timedelta

    from market.services.close_learn import compute_skill_metrics

    active = MLModelVersion.objects.filter(model_name="next_close_rf", is_active=True).order_by("-trained_at").first()
    since = timezone.localdate() - timedelta(days=CLOSE_LEARN_SKILL_WINDOW_DAYS)
    skill = compute_skill_metrics(since=since)
    n = skill.get("n") or 0
    skill_vs_naive = skill.get("skill_vs_naive")
    has_edge = n >= MIN_CLOSE_LEARN_SETTLED and skill_vs_naive is not None and skill_vs_naive > 0
    return {
        "n": n,
        "skill_vs_naive": skill_vs_naive,
        "beats_naive_pct": skill.get("beats_naive_pct"),
        "direction_hit_rate": skill.get("direction_hit_rate"),
        "model_mape": skill.get("model_mape"),
        "baseline_mape": skill.get("baseline_mape"),
        "baseline": "naive (tomorrow's close = today's close)",
        "has_edge": has_edge,
        "scope": f"live_settled_forecasts_last_{CLOSE_LEARN_SKILL_WINDOW_DAYS}d",
        "version": active.version if active is not None else None,
    }


def evaluate_edge(ml_status: dict, close_status: dict) -> tuple[bool, str]:
    """Whether *any* learned layer currently demonstrates a positive,
    adequately-sampled edge, and a plain-language reason either way."""
    ml_ok = ml_status.get("has_edge", False)
    close_ok = close_status.get("has_edge", False)

    if ml_ok and close_ok:
        return True, (
            f"Both the up/down classifier (skill {ml_status['skill_vs_naive']:.2f} vs. {ml_status['baseline']}) "
            f"and the next-close learner (skill {close_status['skill_vs_naive']:.2f} on {close_status['n']} settled "
            f"forecasts) show positive out-of-sample skill vs. their naive baselines."
        )
    if ml_ok:
        return True, (
            f"Up/down classifier shows positive walk-forward skill ({ml_status['skill_vs_naive']:.2f}) vs. "
            f"a baseline of {ml_status['baseline']}, trained on {ml_status['train_rows']} rows."
        )
    if close_ok:
        return True, (
            f"Next-close learner beats its naive baseline (skill {close_status['skill_vs_naive']:.2f}) on "
            f"{close_status['n']} settled forecasts."
        )

    reasons: list[str] = []
    if not ml_status.get("deployed"):
        reasons.append("no up/down classifier is currently deployed (walk-forward skill was not positive)")
    elif (ml_status.get("skill_vs_naive") or 0) <= 0:
        reasons.append("the deployed classifier's walk-forward skill vs. naive is not positive")
    elif (ml_status.get("train_rows") or 0) < MIN_ML_TRAIN_ROWS:
        reasons.append(f"the classifier was trained on only {ml_status.get('train_rows')} rows (need >= {MIN_ML_TRAIN_ROWS})")

    if (close_status.get("n") or 0) < MIN_CLOSE_LEARN_SETTLED:
        reasons.append(f"only {close_status.get('n') or 0} next-close forecasts have settled (need >= {MIN_CLOSE_LEARN_SETTLED})")
    elif (close_status.get("skill_vs_naive") or 0) <= 0:
        reasons.append("the next-close learner's skill vs. naive is not positive")

    return False, "No demonstrated predictive edge: " + "; ".join(reasons) + "."


def data_freshness(analysis: AnalysisResult | None, stock: Stock) -> dict:
    cutoff = analysis.as_of if analysis else None
    if cutoff is None:
        cutoff = stock.prices.live().order_by("-date").values_list("date", flat=True).first()
    if cutoff is None:
        return {"data_cutoff": None, "age_days": None, "is_stale": True, "note": "No price data recorded for this stock yet."}
    age = (timezone.localdate() - cutoff).days
    stale = age > STALE_DATA_DAYS
    note = f"Latest data is from {cutoff.isoformat()}."
    if stale:
        note += f" That is {age} days old — treat this signal as stale until fresh data arrives."
    return {"data_cutoff": cutoff.isoformat(), "age_days": age, "is_stale": stale, "note": note}


def liquidity_note(stock: Stock) -> dict:
    vol = stock.last_volume
    if not vol:
        return {"last_volume": None, "label": "unknown", "note": "No recent volume on record — liquidity unknown."}
    if vol < 10_000:
        label = "thin"
    elif vol < 100_000:
        label = "moderate"
    else:
        label = "active"
    return {
        "last_volume": vol,
        "label": label,
        "note": f"Recent traded volume only ({label}) — not a verified liquidity or free-float assessment.",
    }


def invalidation_condition(analysis: AnalysisResult | None, tech) -> str:
    if analysis is None:
        return "No active signal to invalidate."
    support = getattr(tech, "support", None) if tech else None
    resistance = getattr(tech, "resistance", None) if tech else None
    if analysis.action == "BUY":
        if support:
            return f"Would be reconsidered if price closes back below support (~{support:.2f}) or the RSI/MACD conditions that generated this signal reverse."
        return "Would be reconsidered if the RSI/MACD conditions that generated this signal reverse."
    if analysis.action == "SELL":
        if resistance:
            return f"Would be reconsidered if price reclaims resistance (~{resistance:.2f}) or the RSI/MACD conditions that generated this signal reverse."
        return "Would be reconsidered if the RSI/MACD conditions that generated this signal reverse."
    return "Neutral read — no directional condition to invalidate; re-evaluate after the next few sessions."


def signal_composition(stock: Stock, analysis: AnalysisResult | None) -> dict:
    technical_inputs = [
        "RSI(14)", "MACD histogram", "SMA20/50 trend stack", "20-day momentum",
        "volume vs. 20-day average", "candlestick/chart patterns",
    ]
    if analysis is not None and analysis.ml_score is not None:
        technical_inputs.append("forward-return ML classifier")
    fundamental_inputs = ["P/E ratio (soft score adjustment only)"] if stock.pe_ratio else []
    return {
        "technical_inputs": technical_inputs,
        "fundamental_inputs": fundamental_inputs,
        "note": (
            "Technical/statistical research signal only. No verified liquidity, governance, or "
            "full fundamental-statement analysis is performed — nothing here is a safety, "
            "liquidity, or governance certification."
        ),
    }


def build_signal_status(stock: Stock, analysis: AnalysisResult | None, ml_status: dict, close_status: dict, tech=None) -> dict:
    """Compose the consolidated status from already-fetched ml/close
    statuses. Split out from signal_status() so callers rendering many
    stocks at once (list APIs, digests) can fetch ml_model_status()
    per-exchange and close_learn_edge_status() once, then reuse them
    across every row instead of re-querying/re-scoring per stock."""
    has_edge, edge_reason = evaluate_edge(ml_status, close_status)
    return {
        "has_edge": has_edge,
        "edge_reason": edge_reason,
        "ml_model": ml_status,
        "next_close_model": close_status,
        "freshness": data_freshness(analysis, stock),
        "liquidity": liquidity_note(stock),
        "invalidation": invalidation_condition(analysis, tech),
        "signal_composition": signal_composition(stock, analysis),
    }


def signal_status(stock: Stock, analysis: AnalysisResult | None, tech=None) -> dict:
    """Consolidated status for one stock's forecast/signal presentation —
    the single source the web page, API, and notifications all read from
    so their language stays consistent. Fetches ml/close status fresh;
    for rendering many stocks at once, use build_signal_status() with
    precomputed statuses instead to avoid N+1 queries/rescoring."""
    ml_status = ml_model_status(stock.exchange)
    close_status = close_learn_edge_status()
    return build_signal_status(stock, analysis, ml_status, close_status, tech=tech)


def market_edge_status() -> dict:
    """Market-wide (exchange-agnostic) edge summary for notifications/
    digests, which can't afford a per-stock ML lookup. Uses the combined-
    scope model if present, otherwise DSE's (DSE and CSE are evaluated
    the same way, so either is representative of "is anything deployed
    right now")."""
    ml_status = ml_model_status("DSE")
    close_status = close_learn_edge_status()
    has_edge, edge_reason = evaluate_edge(ml_status, close_status)
    return {"has_edge": has_edge, "edge_reason": edge_reason}
