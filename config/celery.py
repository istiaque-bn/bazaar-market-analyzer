import os

from celery import Celery
from celery.schedules import crontab
from celery.signals import task_postrun, task_prerun

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")

app = Celery("bazaar")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@task_prerun.connect
def _tag_task_logging_context(task_id=None, task=None, **kwargs):
    """Phase 9: so every log line a task emits is tagged with its task_id/
    name (see config/logging_utils.py), the same way RequestIDMiddleware
    tags web requests."""
    from config.logging_utils import task_id_var, task_name_var

    task_id_var.set(task_id or "-")
    task_name_var.set(getattr(task, "name", None) or "-")


@task_postrun.connect
def _clear_task_logging_context(**kwargs):
    from config.logging_utils import task_id_var, task_name_var

    task_id_var.set("-")
    task_name_var.set("-")

# Celery crontab: 0=Sunday … 6=Saturday (like cron)
# Bangladesh market week: Sunday–Thursday
_BD_WEEK = "0-4"

app.conf.beat_schedule = {
    # Live DSE/CSE quote sync. Ticks every 60s every day; the task itself
    # (market.services.autosync.maybe_sync) no-ops unless
    # AUTO_SYNC_INTERVAL_MARKET/_OFF has actually elapsed, so this one
    # fixed-interval entry covers both market-hours and off-hours cadence
    # without separate schedules per session state.
    "sync-live-market": {
        "task": "market.tasks.sync_live_market",
        "schedule": 60.0,
    },
    # Automatic daily append — no dashboard Fetch button required
    "append-market-1005": {
        "task": "market.tasks.append_daily_bars",
        "schedule": crontab(hour=10, minute=5, day_of_week=_BD_WEEK),
    },
    "append-market-1405": {
        "task": "market.tasks.append_daily_bars",
        "schedule": crontab(hour=14, minute=5, day_of_week=_BD_WEEK),
    },
    # After-close settlement: settle due next-day-close forecasts, retrain
    # the close-learn model if anything settled, generate new forecasts.
    "close-learn-settlement-1445": {
        "task": "market.tasks.close_learn_settlement",
        "schedule": crontab(hour=14, minute=45, day_of_week=_BD_WEEK),
    },
    # Standalone forward-return model retrain (independent of analysis).
    "train-ml-model-1450": {
        "task": "market.tasks.train_ml_model",
        "schedule": crontab(hour=14, minute=50, day_of_week=_BD_WEEK),
    },
    "send-daily-digest": {
        "task": "notifications.tasks.send_daily_digest",
        "schedule": crontab(hour=15, minute=0, day_of_week=_BD_WEEK),
    },
    # ML Reliability Monitor: capture today's predictions, settle due
    # outcomes, assess both model families against rolling windows. Runs
    # after train-ml-model/close-learn-settlement/digest so the day's
    # AnalysisResult/NextDayCloseForecast rows already exist to capture from.
    "assess-ml-reliability-1520": {
        "task": "market.tasks.assess_ml_reliability",
        "schedule": crontab(hour=15, minute=20, day_of_week=_BD_WEEK),
    },
    # Refresh the DSE holiday calendar (market.models.MarketHoliday) so next
    # month's holidays are on record before they're needed. Fires daily on
    # the 28th-31st at 23:30; the task itself only proceeds on the actual
    # last day of the month (crontab can't express that directly — see
    # market.tasks.sync_holiday_calendar_task).
    "sync-holiday-calendar-monthly": {
        "task": "market.tasks.sync_holiday_calendar",
        "schedule": crontab(hour=23, minute=30, day_of_month="28-31"),
    },
}
