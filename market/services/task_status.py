"""Persist a success/failure record for each background-task execution."""
from __future__ import annotations

from functools import wraps

from django.utils import timezone

from market.models import TaskRun, TaskStatus


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
                raise
            run.status = _status_for_result(result)
            run.detail = result if isinstance(result, dict) else {"result": result}
            run.finished_at = timezone.now()
            run.save(update_fields=["status", "detail", "finished_at"])
            return result

        return wrapper

    return decorator
