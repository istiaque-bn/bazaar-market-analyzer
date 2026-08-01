"""Persist a success/failure record for each background-task execution."""
from __future__ import annotations

from functools import wraps

from django.utils import timezone

from market.models import TaskRun, TaskStatus


def record_task_run(task_name: str):
    """Decorator: create a TaskRun row for each call, mark it success/failure,
    and store the return value (or error) — independent of Celery's own
    opaque Redis result backend, so status is inspectable in Django admin."""

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
            run.status = TaskStatus.SUCCESS
            run.detail = result if isinstance(result, dict) else {"result": result}
            run.finished_at = timezone.now()
            run.save(update_fields=["status", "detail", "finished_at"])
            return result

        return wrapper

    return decorator
