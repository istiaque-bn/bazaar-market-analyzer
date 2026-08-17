"""
Phase 9: operational metrics — the numbers the alert thresholds
(market.services.ops_alerts) and the staff ops report page both read.
Nothing here writes to the database; it's read-only aggregation over
data other phases already persist (TaskRun from Phase "background
tasks", quality_flags/ImportBatch from Phase 6, MLModelVersion/
close-learn skill from Phases 4/7).
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from market.models import AnalysisResult, Exchange, TaskRun, TaskStatus

TASK_METRICS_WINDOW_DAYS = 7
PREDICTION_VOLUME_TREND_DAYS = 7


def task_health(window_days: int = TASK_METRICS_WINDOW_DAYS) -> list[dict]:
    """Per-task-name run counts, failure rate, and average duration over
    the trailing window, plus the single most recent run's outcome (so a
    task that's currently failing shows red even if its historical
    failure rate is low)."""
    since = timezone.now() - timedelta(days=window_days)
    runs = list(TaskRun.objects.filter(started_at__gte=since).order_by("task_name", "-started_at"))
    by_task: dict[str, list[TaskRun]] = {}
    for run in runs:
        by_task.setdefault(run.task_name, []).append(run)

    out = []
    for task_name, task_runs in sorted(by_task.items()):
        total = len(task_runs)
        failures = sum(1 for r in task_runs if r.status == TaskStatus.FAILURE)
        durations = [
            (r.finished_at - r.started_at).total_seconds()
            for r in task_runs
            if r.finished_at is not None
        ]
        latest = task_runs[0]
        out.append(
            {
                "task_name": task_name,
                "runs": total,
                "failures": failures,
                "failure_rate": round(failures / total, 3) if total else None,
                "avg_duration_seconds": round(sum(durations) / len(durations), 2) if durations else None,
                "latest_status": latest.status,
                "latest_started_at": latest.started_at.isoformat(),
                "latest_error": latest.error[:300] if latest.status == TaskStatus.FAILURE else "",
            }
        )
    return out


def prediction_volume() -> dict:
    """How many signals exist for the most recent scored session, plus a
    short daily trend — a silent drop to zero (a broken analysis task
    that stops erroring but also stops producing rows) is otherwise
    invisible to failure-count-based alerting."""
    latest_as_of = AnalysisResult.objects.order_by("-as_of").values_list("as_of", flat=True).first()
    trend = list(
        AnalysisResult.objects.order_by("-as_of")
        .values("as_of")
        .distinct()[:PREDICTION_VOLUME_TREND_DAYS]
    )
    trend_counts = []
    for row in trend:
        as_of = row["as_of"]
        trend_counts.append({"as_of": as_of.isoformat(), "count": AnalysisResult.objects.filter(as_of=as_of).count()})
    return {
        "latest_as_of": latest_as_of.isoformat() if latest_as_of else None,
        "latest_count": AnalysisResult.objects.filter(as_of=latest_as_of).count() if latest_as_of else 0,
        "trend": list(reversed(trend_counts)),
    }


def rejected_rows_summary() -> dict:
    """Reuses Phase 6's already-computed quality_flags rather than
    re-scanning 600k+ rows on every ops-page view."""
    from market.services.data_quality import provenance_report

    report = provenance_report()
    return {
        "flagged_rows": report["flagged_rows"],
        "flag_counts": report["flag_counts"],
        "freshness": report["freshness"],
    }


def model_evaluation_summary() -> dict:
    """Current deployment status + walk-forward/live skill for both
    learned layers (Phase 4's forward-return classifier, Phase 7/close-
    learn's next-close learner) — "model drift and evaluation
    performance" per exchange."""
    from market.services.signal_status import close_learn_edge_status, ml_model_status

    return {
        "forward_return_model": {ex: ml_model_status(ex) for ex in (Exchange.DSE, Exchange.CSE)},
        "next_close_model": close_learn_edge_status(),
    }


OPS_SUMMARY_CACHE_KEY = "ops_metrics:ops_summary"
OPS_SUMMARY_CACHE_SECONDS = 60


def ops_summary() -> dict:
    """Everything the staff ops report page and the alert-threshold scan
    need, gathered once.

    Cached for OPS_SUMMARY_CACHE_SECONDS: rejected_rows_summary() alone
    scans the ~640k-row PriceHistory table, and task_health()/
    model_evaluation_summary() each add a couple more seconds — every
    admin/staff panel load, the ops report page, and the periodic Telegram
    alert task were each independently paying that full cost. None of
    these consumers need per-request freshness; a task list that runs
    every 1-5 minutes doesn't change meaningfully within a minute.
    """
    from django.core.cache import cache

    from market.services.exchange_config import enabled_exchanges

    cached = cache.get(OPS_SUMMARY_CACHE_KEY)
    if cached is not None:
        return cached

    result = {
        "generated_at": timezone.now().isoformat(),
        "tasks": task_health(),
        "predictions": prediction_volume(),
        "rejected_rows": rejected_rows_summary(),
        "models": model_evaluation_summary(),
        "enabled_exchanges": enabled_exchanges(),
    }
    cache.set(OPS_SUMMARY_CACHE_KEY, result, OPS_SUMMARY_CACHE_SECONDS)
    return result
