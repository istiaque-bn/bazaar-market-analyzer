"""Telegram ML daily report — deterministic, plain-language translation of
stored ML evidence for a non-technical Admin. Every number here is read
from data other parts of the project already compute (MLModelVersion's
walk-forward training metrics, ReliabilityAssessment's live/settled
metrics — see market.services.reliability_report/reliability_metrics);
nothing is recomputed or invented here, and nothing is written — this
module is pure read + plain-language formatting.

Two precision numbers are kept explicitly separate throughout, per the
governing spec:
  - "historical" = out-of-sample walk-forward test-fold precision, from
    MLModelVersion.metrics (recorded once, at training time).
  - "live" = precision over settled real-world predictions, from the
    latest ReliabilityAssessment (recomputed daily as more predictions
    settle) — see market.services.reliability_report.run_reliability_assessment,
    scheduled at 15:20 Asia/Dhaka.
"""
from __future__ import annotations

from datetime import date

from django.utils import timezone

from market.models import Exchange, MLModelVersion, PredictionSnapshot, ReliabilityAssessment
from market.services.exchange_config import enabled_exchanges
from market.services.ml_model import FORWARD_HORIZON_TRADING_DAYS
from market.services.ml_model import MODEL_NAME as FORWARD_MODEL_NAME
from market.services.ml_training import active_model_version
from market.services.reliability_metrics import HIGH_CALIBRATION_ERROR, MIN_SAMPLES_WATCH
from market.services.trading_calendar import closure_reason

# The largest configured reliability window — the closest this project
# gets to "all-time" live evidence (see reliability_metrics.WINDOW_SIZES);
# reused rather than recomputed, per the "do not duplicate the ML
# reliability calculations" instruction.
REPORT_WINDOW_LABEL = "365"

MODEL_FRIENDLY_NAMES = {
    "forward_return_rf": "Direction model",
    "next_close_rf": "Next-session estimate model",
}

# No successful training in this many days is flagged as "stale" and
# suggests a retraining review — generous vs. the daily training
# schedule (a model only stays this old if training has genuinely had
# nothing new to learn from, or is quietly failing).
STALE_MODEL_DAYS = 14

# Live-precision swings smaller than this (percentage points) are
# described as "stable" rather than "improved"/"declined".
COMPARISON_TOLERANCE_PCT = 3.0

EVIDENCE_BANDS = (
    (300, "Stronger evidence"),
    (100, "Moderate"),
    (30, "Limited"),
    (0, "Very limited"),
)

DISCLAIMER = "Historical and live results do not guarantee future returns."

_ACTION_BY_STATUS = {
    "Stable": "Keep the current model active.",
    "Promising": "Keep the current model active, but do not increase confidence yet.",
    "Experimental": "Keep this model experimental — collect more completed predictions before trusting results.",
    "Weak": "Keep monitoring closely; avoid increasing reliance on this model's signals right now.",
    "Declining": "Pause increasing reliance on this model and review recent predictions.",
    "Suspended": "No model is currently approved for confident use.",
    "No evidence": "No model is currently approved for confident use.",
}


def _local_date(dt) -> date:
    return timezone.localtime(dt).date()


def evidence_label(n: int) -> str:
    """Translate a settled-prediction sample size into a plain-language
    evidence label per the documented thresholds — never a bare number,
    never a claim of proof."""
    if n <= 0:
        return "No evidence"
    for threshold, label in EVIDENCE_BANDS:
        if n >= threshold:
            return label
    return "Very limited"


def _pct(fraction: float | None) -> int | None:
    return round(fraction * 100) if fraction is not None else None


def _friendly_model_name(model_name: str) -> str:
    return MODEL_FRIENDLY_NAMES.get(model_name, model_name)


def _resolve_scope(model_name: str) -> tuple[str, MLModelVersion | None]:
    """Prefer the combined-scope active model; fall back to the first
    enabled exchange with its own active model. If nothing is active
    anywhere, report on whichever scope was most recently trained (so a
    never-promoted/deactivated candidate is still findable for the
    "Suspended"/"candidate failed gate" status branches) rather than
    guessing a scope nothing was ever trained under."""
    combined = active_model_version(model_name, exchange_scope="combined")
    if combined is not None:
        return "combined", combined
    exchanges = enabled_exchanges()
    for ex in exchanges:
        m = active_model_version(model_name, exchange_scope=ex)
        if m is not None:
            return ex, m
    latest_any = MLModelVersion.objects.filter(model_name=model_name).order_by("-trained_at").first()
    if latest_any is not None:
        return latest_any.exchange_scope, None
    return (exchanges[0] if exchanges else "combined"), None


def _latest_training_version(model_name: str, exchange_scope: str) -> MLModelVersion | None:
    return MLModelVersion.objects.filter(model_name=model_name, exchange_scope=exchange_scope).order_by("-trained_at").first()


def _latest_assessment(family: str, exchange: str, horizon: int, window_label: str = REPORT_WINDOW_LABEL) -> ReliabilityAssessment | None:
    return (
        ReliabilityAssessment.objects.filter(model_family=family, exchange=exchange, horizon_trading_days=horizon, window_label=window_label)
        .order_by("-run_at")
        .first()
    )


def _data_quality_flags(exchanges: list[str]) -> list[str]:
    """Reuses the existing operational-alerts evaluation (market.services.
    ops_alerts) rather than a second data-quality check — only the
    subset relevant to whether today's ML evidence should be trusted."""
    from market.services import ops_alerts

    try:
        alerts = ops_alerts.evaluate_alerts()
    except Exception:
        return []
    relevant = {f"stale_data_{ex}" for ex in exchanges} | {"stale_analysis"}
    return [a["message"] for a in alerts if a["key"] in relevant or a["key"].startswith("silent_sync_failure_")]


def determine_status(
    *,
    active_model: MLModelVersion | None,
    candidate_failed: bool,
    never_had_active: bool,
    assessment: ReliabilityAssessment | None,
    live_n: int,
    evidence: str,
) -> tuple[str, str]:
    """Deterministic status assignment — no LLM, no free-form judgment.
    Every branch cites the stored evidence (deployment status, live
    ReliabilityAssessment.status, sample size) that produced it."""
    if active_model is None:
        if never_had_active:
            return (
                "Suspended",
                "No ML model is currently approved — a previously trained candidate did not qualify. "
                "Bazaar is using its rule-based analysis.",
            )
        return "No evidence", "No ML model is currently approved. Bazaar is using its rule-based analysis."

    if assessment is None or live_n == 0:
        label, sentence = "Experimental", "The model is active, but there is not enough completed live evidence yet to judge real-world performance."
    elif assessment.status == ReliabilityAssessment.Status.INSUFFICIENT_DATA:
        label, sentence = "Promising", "Promising, but live evidence is still limited."
    elif assessment.status == ReliabilityAssessment.Status.HEALTHY:
        if evidence in ("Moderate", "Stronger evidence"):
            label, sentence = "Stable", "Performance is stable, with a reasonable amount of live evidence behind it."
        else:
            label, sentence = "Promising", "Promising, but live evidence is still limited."
    elif assessment.status == ReliabilityAssessment.Status.WATCH:
        label, sentence = "Weak", "Results are mixed right now — treat this model's signals cautiously."
    else:  # DEGRADED / CRITICAL
        label, sentence = "Declining", "Recent results have weakened. Review the model before relying on new signals."

    if candidate_failed:
        sentence += " A newer candidate was trained recently but did not beat the simple comparison, so the current model stays in place."

    return label, sentence


def generate_recommendations(
    *,
    assessment: ReliabilityAssessment | None,
    candidate_failed: bool,
    calibration_error: float | None,
    live_n: int,
    stale_model: bool,
    data_quality_flags: list[str],
) -> list[str]:
    """Up to 3 recommendations, most-urgent first, each mapped from a
    concrete detected condition — never a fixed "always retrain"
    suggestion. See the module docstring for why the mappings are what
    they are."""
    recs: list[str] = []

    if assessment is not None and assessment.status in (ReliabilityAssessment.Status.DEGRADED, ReliabilityAssessment.Status.CRITICAL):
        recs.append("Pause automatic promotion and investigate recent predictions.")

    if candidate_failed:
        recs.append("Keep the existing approved model and investigate the failed candidate.")

    if data_quality_flags:
        recs.append("Fix missing, stale or unadjusted market data before retraining.")

    if calibration_error is not None and calibration_error > HIGH_CALIBRATION_ERROR:
        recs.append("The confidence percentages appear too optimistic or too cautious and need adjustment.")

    if live_n < MIN_SAMPLES_WATCH:
        recs.append("Collect more completed live predictions before changing the model.")

    if stale_model:
        recs.append("Run a controlled retraining review using newly completed data.")

    if not recs:
        recs.append("No specific concerns detected — continue routine monitoring.")

    return recs[:3]


def build_report_context(as_of: date | None = None) -> dict:
    """Pure, read-only. Everything render_report_sections needs, gathered
    once so it can be unit-tested/previewed without touching Telegram."""
    as_of = as_of or timezone.localdate()
    exchanges = enabled_exchanges()
    report_exchange = exchanges[0] if exchanges else Exchange.DSE

    scope, active_model = _resolve_scope(FORWARD_MODEL_NAME)
    latest_training = _latest_training_version(FORWARD_MODEL_NAME, scope)

    never_had_active = active_model is None and latest_training is not None
    candidate_failed = bool(
        latest_training is not None
        and active_model is not None
        and latest_training.id != active_model.id
        and not latest_training.is_active
        and latest_training.trained_at > active_model.trained_at
    )
    trained_today = latest_training is not None and _local_date(latest_training.trained_at) == as_of

    assessment = None
    if active_model is not None:
        assessment = _latest_assessment(PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF, report_exchange, FORWARD_HORIZON_TRADING_DAYS)

    hist_model_metrics = (active_model.metrics or {}).get("model") if active_model else None
    hist_n = (hist_model_metrics or {}).get("n") or 0
    hist_precision = (hist_model_metrics or {}).get("precision") if hist_n else None
    hist_positive_rate = (hist_model_metrics or {}).get("positive_rate_pred") if hist_n else None

    live_metrics = ((assessment.metrics or {}).get("classification") or {}).get("model") if assessment else None
    live_n = assessment.sample_count if assessment else 0
    live_precision = (live_metrics or {}).get("precision") if live_n else None
    live_positive_rate = (live_metrics or {}).get("positive_rate_pred") if live_n else None
    live_accuracy = (live_metrics or {}).get("accuracy") if live_n else None
    live_correct = round(live_accuracy * live_n) if (live_accuracy is not None and live_n) else None
    calibration_error = (live_metrics or {}).get("calibration_error") if live_n else None

    evidence = evidence_label(live_n)
    status_label, status_sentence = determine_status(
        active_model=active_model,
        candidate_failed=candidate_failed,
        never_had_active=never_had_active,
        assessment=assessment,
        live_n=live_n,
        evidence=evidence,
    )

    data_quality_flags = _data_quality_flags(exchanges)
    stale_model = active_model is not None and (as_of - _local_date(active_model.trained_at)).days > STALE_MODEL_DAYS

    recommendations = generate_recommendations(
        assessment=assessment,
        candidate_failed=candidate_failed,
        calibration_error=calibration_error,
        live_n=live_n,
        stale_model=stale_model,
        data_quality_flags=data_quality_flags,
    )

    close_scope, close_active = _resolve_scope("next_close_rf")

    return {
        "as_of": as_of,
        "closure_reason": closure_reason(as_of),
        "exchange": report_exchange,
        "scope": scope,
        "active_model": active_model,
        "model_version_tag": active_model.version if active_model else None,
        "window_label": REPORT_WINDOW_LABEL,
        "latest_training": latest_training,
        "candidate_failed": candidate_failed,
        "never_had_active": never_had_active,
        "trained_today": trained_today,
        "assessment": assessment,
        "historical": {"n": hist_n, "precision": hist_precision, "positive_rate": hist_positive_rate},
        "live": {
            "n": live_n,
            "precision": live_precision,
            "positive_rate": live_positive_rate,
            "accuracy": live_accuracy,
            "correct": live_correct,
            "calibration_error": calibration_error,
        },
        "latest_settled_outcome": assessment.period_end if assessment else None,
        "evidence": evidence,
        "status_label": status_label,
        "status_sentence": status_sentence,
        "recommendations": recommendations,
        "stale_model": stale_model,
        "data_quality_flags": data_quality_flags,
        "close_learn": {"scope": close_scope, "active": close_active is not None, "model": close_active},
    }


def _close_learn_note(context: dict) -> str | None:
    """Next-session estimate model only gets its own line when its status
    differs materially from the direction model's (both active, or both
    not) — see the spec's "Only add separate sections when their
    statuses differ materially."""
    direction_active = context["active_model"] is not None
    close_active = context["close_learn"]["active"]
    if direction_active == close_active:
        return None
    friendly = _friendly_model_name("next_close_rf")
    return f"{friendly}: {'active' if close_active else 'not currently active'} (separate from the direction model above)."


def render_report_sections(context: dict, comparison: dict | None = None) -> list[str]:
    """Ordered plain-language sections — a list so callers can split at
    section boundaries for Telegram's message-length limit without ever
    cutting mid-sentence."""
    as_of = context["as_of"]
    sections = [f"\U0001f4ca Bazaar ML Daily Report\nDate: {as_of.strftime('%-d %B %Y')}"]

    if context["closure_reason"]:
        sections.append(f"Today is a non-trading day ({context['closure_reason']}) — a shorter check-in report follows.")

    sections.append(f"Overall status: {context['status_sentence']}")

    active_model = context["active_model"]
    current_lines = ["Current model"]
    if active_model:
        days_ago = (as_of - _local_date(active_model.trained_at)).days
        when = "today" if days_ago <= 0 else ("yesterday" if days_ago == 1 else f"{days_ago} days ago")
        current_lines.append(f"• Model trained: {when} ({_local_date(active_model.trained_at)})")
        current_lines.append(f"• Training-data cutoff: {active_model.data_cutoff}")
        current_lines.append(f"• Current use: {context['status_label']}")
    else:
        current_lines.append("• No ML model is currently approved for confident use.")
    current_lines.append(f"• Market: {context['exchange']}")
    current_lines.append(f"• New training today: {'Yes' if context['trained_today'] else 'No'}")
    sections.append("\n".join(current_lines))

    settled = context.get("latest_settled_outcome")
    if settled:
        sections.append(f"Live-evidence status: latest settled outcome in this report is {settled}. New training and new settled evidence are measured separately.")

    if active_model:
        acc_lines = ["How accurate is it?"]
        hist, live = context["historical"], context["live"]
        if hist["n"] and hist["positive_rate"]:
            acc_lines.append(f"• Historical test: when the model suggested a rise, it was correct about {_pct(hist['precision'])} times out of 100.")
            acc_lines.append(f"• This is based on {hist['n']} historical test predictions.")
        elif hist["n"]:
            acc_lines.append("• The model did not predict a rise in the historical test set, so historical precision cannot be calculated.")
        else:
            acc_lines.append("• The model has not been tested enough yet to report historical accuracy.")

        if live["n"] == 0:
            acc_lines.append("• There is not enough completed live evidence to calculate a meaningful result yet.")
        else:
            if live["positive_rate"]:
                acc_lines.append(f"• Live so far: when the model suggested a rise, it was correct about {_pct(live['precision'])} times out of 100.")
            else:
                acc_lines.append("• The model has not predicted a rise in live trading yet, so live precision cannot be calculated.")
            if live["correct"] is not None:
                acc_lines.append(f"• The predicted direction was correct in {live['correct']} out of {live['n']} completed live predictions.")
            acc_lines.append(f"• Evidence level: {context['evidence']}")
        sections.append("\n".join(acc_lines))

    close_note = _close_learn_note(context)
    if close_note:
        sections.append(close_note)

    change_lines = ["What changed?"]
    if comparison:
        change_lines.append(f"• {comparison['message']}")
    else:
        change_lines.append("• This is the first report on record — nothing to compare yet.")
    sections.append("\n".join(change_lines))

    rec_lines = ["What should be improved?"]
    for i, rec in enumerate(context["recommendations"], start=1):
        rec_lines.append(f"{i}. {rec}")
    sections.append("\n".join(rec_lines))

    action_lines = ["Recommended action"]
    action_lines.append(f"• {_ACTION_BY_STATUS.get(context['status_label'], 'Continue routine monitoring.')}")
    from django.conf import settings

    if getattr(settings, "AUTO_ML_TRAINING", True):
        action_lines.append(f"• Next scheduled training check: tomorrow at {getattr(settings, 'AUTO_ML_TRAINING_TIME', '00:30')} Asia/Dhaka.")
    sections.append("\n".join(action_lines))

    sections.append(DISCLAIMER)
    return sections


def split_for_telegram(sections: list[str], limit: int = 3500) -> list[str]:
    """Greedily packs whole sections into chunks under Telegram's message
    length limit, never splitting a section mid-sentence, always
    preserving order."""
    chunks: list[str] = []
    current = ""
    for section in sections:
        candidate = f"{current}\n\n{section}" if current else section
        if len(candidate) > limit and current:
            chunks.append(current)
            current = section
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def render_report_text(as_of: date | None = None, comparison: dict | None = None) -> tuple[dict, list[str]]:
    """Convenience wrapper: build context + render sections in one call —
    used directly by the Admin preview view (no Celery, no Telegram)."""
    context = build_report_context(as_of=as_of)
    return context, render_report_sections(context, comparison=comparison)
