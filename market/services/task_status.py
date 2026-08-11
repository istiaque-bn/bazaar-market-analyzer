"""Persist a success/failure record for each background-task execution."""
from __future__ import annotations

from functools import wraps
from datetime import timedelta

from django.utils import timezone

from market.models import TaskAlertState, TaskHealth, TaskHealthStatus, TaskRun, TaskStatus


# A hard time limit kills the Celery child process immediately.  Code in the
# task's ``except`` block then cannot run, leaving its durable TaskRun row as
# ``started`` forever.  The reconciler below is deliberately independent of
# Celery's result backend so operations status can heal after that failure.
TASK_HARD_LIMIT_SECONDS = {
    "market.tasks.sync_live_market": 90,
    "market.tasks.run_intraday_analysis": 120,
    "market.tasks.fetch_all_market_data": 600,
    "market.tasks.run_full_analysis": 900,
    "market.tasks.append_daily_bars": 600,
    "market.tasks.train_ml_model": 600,
    "market.tasks.close_learn_settlement": 600,
    "market.tasks.assess_ml_reliability": 600,
    "market.tasks.run_end_of_day_pipeline": 1800,
}
ORPHAN_GRACE_SECONDS = 120


def _status_for_result(result) -> str:
    """An exception always means FAILURE (handled separately below); a
    normal return only means SUCCESS by default. Many task-level
    services already return a self-describing dict for the "nothing went
    wrong, but there was nothing to do" case — market closed, disabled
    exchange, duplicate run, lock already held (see market.services.
    autosync.maybe_sync, market.services.intraday_analysis, and the
    fetchers' exchange-disabled skip) — this reclassifies those as
    SKIPPED rather than SUCCESS, and an explicit partial-completion dict
    as PARTIAL, so `manage.py`/the admin automation panel/ops alerts can
    tell "ran and did nothing (expected)" apart from "ran and did
    everything (normal)" without re-parsing free-text detail."""
    if isinstance(result, dict):
        if result.get("skipped"):
            return TaskStatus.SKIPPED
        if result.get("partial"):
            return TaskStatus.PARTIAL
    return TaskStatus.SUCCESS


def record_task_run(task_name: str):
    """Decorator: create a TaskRun row for each call, mark it success/
    partial/skipped/failure, and store the return value (or error) —
    independent of Celery's own opaque Redis result backend, so status is
    inspectable in Django admin and the Admin automation panel."""

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            run = TaskRun.objects.create(task_name=task_name, status=TaskStatus.STARTED)
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                run.status = TaskStatus.FAILURE
                run.error = str(exc)[:2000]
                run.finished_at = timezone.now()
                run.save(update_fields=["status", "error", "finished_at"])
                _update_task_health(run)
                raise
            run.status = _status_for_result(result)
            run.detail = result if isinstance(result, dict) else {"result": result}
            run.finished_at = timezone.now()
            run.save(update_fields=["status", "detail", "finished_at"])
            _update_task_health(run)
            return result

        return wrapper

    return decorator


def _update_task_health(run: TaskRun) -> None:
    """Best-effort state transition; monitoring must never hide task outcome."""
    try:
        detail = run.detail if isinstance(run.detail, dict) else {}
        duration = (run.finished_at - run.started_at).total_seconds() if run.finished_at else None
        health, _ = TaskHealth.objects.get_or_create(task_name=run.task_name)
        health.last_run = run.started_at
        health.run_duration_seconds = duration
        health.symbols_attempted = int(detail.get("symbols_attempted", 0) or 0)
        health.symbols_successful = int(detail.get("symbols_successful", 0) or 0)
        health.symbols_failed = int(detail.get("symbols_failed", 0) or 0)
        # Partial completion is operationally healthy: a bad symbol was isolated.
        if run.status in (TaskStatus.SUCCESS, TaskStatus.PARTIAL, TaskStatus.SKIPPED):
            was_unhealthy = health.current_health_status == TaskHealthStatus.UNHEALTHY
            health.last_success = run.finished_at
            health.consecutive_failures = 0
            health.current_health_status = TaskHealthStatus.HEALTHY
            health.last_error = ""
            if was_unhealthy:
                health.alert_state = TaskAlertState.RECOVERY_PENDING
                health.last_recovery = run.finished_at
            elif health.alert_state != TaskAlertState.RECOVERY_PENDING:
                health.alert_state = TaskAlertState.NONE
        elif run.status == TaskStatus.FAILURE:
            health.last_failure = run.finished_at
            health.last_error = run.error[:2000]
            health.consecutive_failures += 1
            if health.consecutive_failures >= 3:
                health.current_health_status = TaskHealthStatus.UNHEALTHY
                if health.alert_state != TaskAlertState.FAILURE_SENT:
                    health.alert_state = TaskAlertState.FAILURE_PENDING
        health.save()
    except Exception:
        # This deliberately avoids converting a successful data task into a failure
        # merely because its monitoring write was unavailable.
        return


def reconcile_orphaned_task_runs(*, now=None) -> dict:
    """Close records whose Celery process could no longer write its result.

    This does not guess about recent jobs: a row is changed only after its
    task-specific hard limit plus a two-minute grace period.  Rows are kept
    for audit purposes and become normal failures with a clear explanation.
    """
    now = now or timezone.now()
    reconciled: list[int] = []
    started = TaskRun.objects.filter(status=TaskStatus.STARTED).order_by("started_at")
    for run in started.iterator():
        hard_limit = TASK_HARD_LIMIT_SECONDS.get(run.task_name, 600)
        cutoff = now - timedelta(seconds=hard_limit + ORPHAN_GRACE_SECONDS)
        if run.started_at > cutoff:
            continue
        updated = TaskRun.objects.filter(pk=run.pk, status=TaskStatus.STARTED).update(
            status=TaskStatus.FAILURE,
            finished_at=now,
            error=(
                "Orphaned task record: no completion was written after the "
                f"{hard_limit}-second hard limit plus grace period; the Celery worker process likely ended."
            ),
        )
        if updated:
            reconciled.append(run.pk)
    return {"ok": True, "reconciled": len(reconciled), "run_ids": reconciled}
