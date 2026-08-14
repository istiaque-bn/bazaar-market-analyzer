"""Read-only lessons from completed paper trades.

This service deliberately reports evidence; it never alters an ML model,
strategy threshold, or account configuration.
"""
from __future__ import annotations

from collections import defaultdict

from django.db.models import Sum

from market.models import PaperLearningFeedback

MIN_SAMPLE_FOR_RECOMMENDATION = 30
MIN_BUCKET_SAMPLE = 10


def _bucket(confidence: float | None) -> str:
    if confidence is None:
        return "No confidence saved"
    if confidence >= 0.80:
        return "80% and above"
    if confidence >= 0.70:
        return "70%–79%"
    return "Below 70%"


def paper_learning_report(feedbacks=None) -> dict:
    """Summarise settled, after-cost paper results without changing anything."""
    feedbacks = list(feedbacks if feedbacks is not None else PaperLearningFeedback.objects.all())
    total = len(feedbacks)
    groups = defaultdict(lambda: {"count": 0, "wins": 0, "net_total": 0.0})
    for feedback in feedbacks:
        group = groups[_bucket(feedback.predicted_confidence)]
        group["count"] += 1
        group["wins"] += int(feedback.profitable_after_costs)
        group["net_total"] += float(feedback.net_return_pct)

    confidence_rows = []
    for label in ("80% and above", "70%–79%", "Below 70%", "No confidence saved"):
        item = groups.get(label)
        if not item:
            continue
        confidence_rows.append({
            "label": label,
            "count": item["count"],
            "win_rate": round(item["wins"] / item["count"] * 100, 1),
            "average_net_return": round(item["net_total"] / item["count"], 2),
        })

    reliable_rows = [row for row in confidence_rows if row["count"] >= MIN_BUCKET_SAMPLE]
    best = max(reliable_rows, key=lambda row: (row["average_net_return"], row["win_rate"])) if reliable_rows else None
    if total < MIN_SAMPLE_FOR_RECOMMENDATION:
        recommendation = (
            f"Still learning: {total}/{MIN_SAMPLE_FOR_RECOMMENDATION} completed trades. "
            "Keep the current rules; there is not enough evidence to change them."
        )
    elif best:
        recommendation = (
            f"Best tested confidence range: {best['label']} "
            f"({best['count']} trades, {best['win_rate']}% after-cost win rate, "
            f"{best['average_net_return']:+.2f}% average net return). Review it before changing any rule."
        )
    else:
        recommendation = "More evenly distributed completed trades are needed before comparing confidence ranges."
    return {
        "completed_trades": total,
        "minimum_sample": MIN_SAMPLE_FOR_RECOMMENDATION,
        "confidence_rows": confidence_rows,
        "recommendation": recommendation,
        "ready_for_review": total >= MIN_SAMPLE_FOR_RECOMMENDATION and best is not None,
    }


def paper_evidence_report(account) -> dict:
    """Auditable, after-cost paper evidence for one account only.

    This is intentionally descriptive.  A positive value never becomes an
    "edge" claim: it must later be compared with a locked out-of-sample
    benchmark under the same cost model.
    """
    feedbacks = list(PaperLearningFeedback.objects.filter(position__account=account))
    learning = paper_learning_report(feedbacks)
    fees = account.trades.aggregate(total=Sum("fee"))["total"] or 0
    snapshots = list(account.equity_snapshots.order_by("as_of").values_list("total_equity", flat=True))
    max_drawdown = None
    if snapshots:
        peak = float(snapshots[0])
        drawdowns = []
        for equity in snapshots:
            value = float(equity)
            peak = max(peak, value)
            drawdowns.append((value / peak - 1) * 100 if peak else 0)
        max_drawdown = round(min(drawdowns), 2)
    gross_total = round(sum(float(item.gross_return_pct) for item in feedbacks), 2)
    net_total = round(sum(float(item.net_return_pct) for item in feedbacks), 2)
    return {
        **learning,
        "total_estimated_fees": fees,
        "gross_return_sum_pct": gross_total,
        "net_return_sum_pct": net_total,
        "max_drawdown_pct": max_drawdown,
        "snapshot_count": len(snapshots),
        "evidence_status": (
            "Insufficient completed trades for review" if len(feedbacks) < MIN_SAMPLE_FOR_RECOMMENDATION
            else "Reviewable paper sample only — not proof of an edge"
        ),
    }
