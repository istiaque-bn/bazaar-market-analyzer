"""
Phase 9: alert thresholds over market.services.ops_metrics /
market.services.health / market.services.signal_status. Pure
evaluation — no delivery. Each alert is a plain dict:
{key, severity ("warning"|"critical"), message, detail}. Callers
(the staff ops page, the `ops_alerts_scan` management command, a
future notification hook) decide what to do with the list; nothing
here pushes to Telegram/email/a third-party service on its own — see
docs/RUNBOOKS.md for why that's a deliberate scope boundary this
phase, not an oversight.
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from market.models import TaskRun, TaskStatus
from market.services import ops_metrics
from market.services.health import check_database

# A normal weekend gap (Thu close -> Sun open) is up to 3 calendar days;
# +1 day of buffer for a holiday before this is "stale" rather than
# "normal non-trading day". Matches signal_status.STALE_DATA_DAYS.
STALE_DATA_DAYS = 4

# How many of a task's most recent runs must all be failures before this
# is "repeated" rather than "one blip".
REPEATED_FAILURE_STREAK = 3
FETCH_TASK_NAMES = (
    "market.tasks.sync_live_market",
    "market.tasks.fetch_all_market_data",
    "market.tasks.append_daily_bars",
)

# Generous vs. the largest per-task time_limit (900s) — a task still
# "started" this long after it began has almost certainly crashed its
# worker process rather than genuinely still be running.
STUCK_JOB_MINUTES = 20


def _stale_data_alerts(freshness: dict) -> list[dict]:
    alerts = []
    today = timezone.localdate()
    for exchange, info in freshness.items():
        latest = info.get("latest_price_date")
        if not latest:
            alerts.append(
                {
                    "key": f"stale_data_{exchange}",
                    "severity": "critical",
                    "message": f"{exchange}: no price data on record at all.",
                    "detail": {"exchange": exchange},
                }
            )
            continue
        age = (today - timezone.datetime.fromisoformat(latest).date()).days
        if age > STALE_DATA_DAYS:
            alerts.append(
                {
                    "key": f"stale_data_{exchange}",
                    "severity": "warning" if age <= STALE_DATA_DAYS * 2 else "critical",
                    "message": f"{exchange}: latest price data is {age} days old (threshold {STALE_DATA_DAYS}).",
                    "detail": {"exchange": exchange, "latest_price_date": latest, "age_days": age},
                }
            )
    return alerts


def _repeated_failure_alerts() -> list[dict]:
    alerts = []
    for task_name in FETCH_TASK_NAMES:
        recent = list(
            TaskRun.objects.filter(task_name=task_name).order_by("-started_at")[:REPEATED_FAILURE_STREAK]
        )
        if len(recent) < REPEATED_FAILURE_STREAK:
            continue
        if all(r.status == TaskStatus.FAILURE for r in recent):
            alerts.append(
                {
                    "key": f"repeated_failure_{task_name}",
                    "severity": "critical",
                    "message": f"{task_name}: last {REPEATED_FAILURE_STREAK} runs all failed.",
                    "detail": {
                        "task_name": task_name,
                        "last_errors": [r.error[:200] for r in recent],
                    },
                }
            )
    return alerts


def _job_overlap_and_stuck_alerts() -> list[dict]:
    alerts = []
    in_flight = list(TaskRun.objects.filter(status=TaskStatus.STARTED).order_by("task_name", "started_at"))
    by_task: dict[str, list[TaskRun]] = {}
    for run in in_flight:
        by_task.setdefault(run.task_name, []).append(run)

    cutoff = timezone.now() - timedelta(minutes=STUCK_JOB_MINUTES)
    for task_name, runs in by_task.items():
        if len(runs) > 1:
            alerts.append(
                {
                    "key": f"job_overlap_{task_name}",
                    "severity": "warning",
                    "message": f"{task_name}: {len(runs)} runs currently in-flight at once.",
                    "detail": {"task_name": task_name, "run_ids": [r.id for r in runs]},
                }
            )
        stuck = [r for r in runs if r.started_at < cutoff]
        if stuck:
            alerts.append(
                {
                    "key": f"stuck_job_{task_name}",
                    "severity": "critical",
                    "message": (
                        f"{task_name}: run #{stuck[0].id} has been 'started' for over "
                        f"{STUCK_JOB_MINUTES} minutes without finishing — likely a crashed worker."
                    ),
                    "detail": {"task_name": task_name, "run_id": stuck[0].id, "started_at": stuck[0].started_at.isoformat()},
                }
            )
    return alerts


def _database_alerts() -> list[dict]:
    if check_database():
        return []
    return [
        {
            "key": "database_unreachable",
            "severity": "critical",
            "message": "Database is not reachable (SELECT 1 failed). See the structured log for the (redacted) exception.",
            "detail": {},
        }
    ]


def _model_degradation_alerts(models: dict) -> list[dict]:
    alerts = []
    for exchange, status in models["forward_return_model"].items():
        if status.get("deployed") and (status.get("skill_vs_naive") or 0) <= 0:
            alerts.append(
                {
                    "key": f"model_degraded_forward_return_{exchange}",
                    "severity": "warning",
                    "message": (
                        f"{exchange} forward-return classifier is deployed but its recorded "
                        f"walk-forward skill vs. naive is {status.get('skill_vs_naive')} (not positive)."
                    ),
                    "detail": {"exchange": exchange, "version": status.get("version")},
                }
            )
    close = models["next_close_model"]
    if (close.get("n") or 0) >= 30 and (close.get("skill_vs_naive") or 0) <= 0:
        alerts.append(
            {
                "key": "model_degraded_next_close",
                "severity": "warning",
                "message": (
                    f"Next-close learner's live skill vs. naive is {close.get('skill_vs_naive')} "
                    f"over {close.get('n')} settled forecasts (not positive)."
                ),
                "detail": {"n": close.get("n"), "skill_vs_naive": close.get("skill_vs_naive")},
            }
        )
    return alerts


def evaluate_alerts(summary: dict | None = None) -> list[dict]:
    """All currently-firing alerts, most-severe first. Pass an
    already-computed ops_metrics.ops_summary() to avoid recomputing it
    (e.g. the ops report page needs both the summary and the alerts
    derived from it, and provenance_report() alone is a real query cost
    over 600k+ rows — see market/services/data_quality.py)."""
    summary = summary if summary is not None else ops_metrics.ops_summary()
    alerts = (
        _stale_data_alerts(summary["rejected_rows"]["freshness"])
        + _repeated_failure_alerts()
        + _job_overlap_and_stuck_alerts()
        + _database_alerts()
        + _model_degradation_alerts(summary["models"])
    )
    severity_rank = {"critical": 0, "warning": 1}
    alerts.sort(key=lambda a: severity_rank.get(a["severity"], 2))
    return alerts
