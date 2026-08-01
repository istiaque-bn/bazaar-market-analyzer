from django.apps import AppConfig


class MarketConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "market"

    # Deliberately does nothing here: no DB queries, no daemon threads.
    # Background work (live sync, daily append, analysis, training,
    # settlement, notifications) runs as scheduled/enqueued Celery tasks —
    # see market/tasks.py and config/celery.py's beat_schedule. Starting
    # threads from ready() doesn't survive multi-process/multi-worker
    # deployment and Django explicitly warns against DB access here.
