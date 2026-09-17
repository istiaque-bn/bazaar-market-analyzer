"""
ML Reliability Monitor — outcome settlement.

Idempotent: only ever touches PredictionSnapshot rows still PENDING, and
each row transitions exactly once (PENDING -> SETTLED or PENDING ->
EXCLUDED), so re-running settle_predictions() on an already-settled
window is always a no-op for those rows. target_date is never
recomputed here — it was already fixed as a real trading session at
capture time (see reliability_capture.nth_trading_day_after); settlement
only checks whether a genuine PriceHistory bar now exists on that exact
date, never a nearby calendar day, so an outcome is never fabricated.

A prediction that has waited past MAX_PENDING_CALENDAR_DAYS with no
valid bar (delisted/suspended stock, permanent data gap) is EXCLUDED
with an explicit reason rather than left pending forever.
"""
from __future__ import annotations

from datetime import date

from django.utils import timezone

from market.models import AdjustmentStatus, PredictionSnapshot, PriceHistory

MAX_PENDING_CALENDAR_DAYS = 30  # generous headroom over any realistic settlement delay
INVALID_SETTLEMENT_FLAGS = {
    "non_positive_close",
    "close_out_of_range",
    "abnormal_jump",
}
MAX_ABS_RETURN_BY_HORIZON = {1: 0.50, 10: 2.00}


def settlement_exclusion_reason(snap: PredictionSnapshot, bar: PriceHistory) -> str:
    """Return a fail-closed reason when a price pair is unsafe to score."""
    reference_bar = PriceHistory.objects.filter(stock=snap.stock, date=snap.data_cutoff_date).first()
    if reference_bar is not None and INVALID_SETTLEMENT_FLAGS.intersection(reference_bar.quality_flags or []):
        return "reference_bar_quality_flag"
    if INVALID_SETTLEMENT_FLAGS.intersection(bar.quality_flags or []):
        return "target_bar_quality_flag"
    if reference_bar is not None:
        statuses = {reference_bar.adjustment_status, bar.adjustment_status}
        if AdjustmentStatus.ADJUSTED in statuses and len(statuses) > 1:
            return "inconsistent_adjustment_status"
    outcome_return = float(bar.close) / float(snap.reference_close) - 1.0
    max_abs_return = MAX_ABS_RETURN_BY_HORIZON.get(snap.horizon_trading_days, 2.0)
    if outcome_return <= -1.0 or abs(outcome_return) > max_abs_return:
        return "implausible_outcome_return"
    return ""


def _exclude(snap: PredictionSnapshot, reason: str) -> None:
    snap.settlement_status = PredictionSnapshot.SettlementStatus.EXCLUDED
    snap.exclusion_reason = reason
    snap.settled_at = timezone.now()
    snap.save(update_fields=["settlement_status", "exclusion_reason", "settled_at"])


def settle_predictions(through_date: date | None = None) -> dict:
    through_date = through_date or timezone.localdate()
    pending = PredictionSnapshot.objects.filter(
        settlement_status=PredictionSnapshot.SettlementStatus.PENDING,
        target_date__isnull=False,
        target_date__lte=through_date,
    ).select_related("stock")

    settled = excluded = 0
    for snap in pending.iterator():
        if snap.stock is None:
            _exclude(snap, "stock_deleted")
            excluded += 1
            continue

        age_days = (through_date - snap.target_date).days
        bar = PriceHistory.objects.filter(stock=snap.stock, date=snap.target_date).first()

        if bar is None or not bar.close:
            if age_days >= MAX_PENDING_CALENDAR_DAYS:
                _exclude(snap, "no_settlement_data_available")
                excluded += 1
            continue  # still within the patience window — try again next run

        if bar.volume is not None and int(bar.volume) <= 0:
            # Suspended-like bar — same convention as close_learn.settle_due_forecasts.
            if age_days >= MAX_PENDING_CALENDAR_DAYS:
                _exclude(snap, "suspended_or_zero_volume")
                excluded += 1
            continue

        if not snap.reference_close:
            _exclude(snap, "invalid_reference_price")
            excluded += 1
            continue

        exclusion_reason = settlement_exclusion_reason(snap, bar)
        if exclusion_reason:
            _exclude(snap, exclusion_reason)
            excluded += 1
            continue

        outcome_price = float(bar.close)
        outcome_return = outcome_price / float(snap.reference_close) - 1.0
        snap.outcome_price = outcome_price
        snap.outcome_return = outcome_return
        snap.outcome_class = outcome_return > 0
        snap.settlement_status = PredictionSnapshot.SettlementStatus.SETTLED
        snap.settled_at = timezone.now()
        snap.save(
            update_fields=["outcome_price", "outcome_return", "outcome_class", "settlement_status", "settled_at"]
        )
        settled += 1

    still_pending = PredictionSnapshot.objects.filter(
        settlement_status=PredictionSnapshot.SettlementStatus.PENDING
    ).count()
    return {
        "ok": True,
        "through_date": through_date.isoformat(),
        "settled": settled,
        "excluded": excluded,
        "still_pending": still_pending,
    }
