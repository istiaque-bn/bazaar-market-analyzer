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
    # without separate schedules per session state. AUTO_MARKET_SYNC=False
    # is honored inside the task itself (maybe_sync), not by removing this
    # entry — same self-throttling pattern as run-intraday-analysis below.
    "sync-live-market": {
        "task": "market.tasks.sync_live_market",
        "schedule": 60.0,
    },
    # Lightweight intraday analysis. Ticks every 60s; the task itself
    # (market.services.intraday_analysis.maybe_run_intraday_analysis)
    # no-ops unless AUTO_INTRADAY_ANALYSIS is on, the market is open,
    # AUTO_INTRADAY_ANALYSIS_INTERVAL has elapsed, and there's newer data
    # to refresh — see that module's docstring.
    "run-intraday-analysis": {
        "task": "market.tasks.run_intraday_analysis",
        "schedule": 60.0,
    },
    # Telegram ML daily report. Ticks every 60s, every day (weekends and
    # holidays included — the report itself explains "no new evidence" on
    # a quiet day rather than skipping). The task
    # (notifications.services.send_ml_daily_report) self-throttles: it
    # no-ops until the current time in TELEGRAM_ML_REPORT_TIMEZONE has
    # reached TELEGRAM_ML_REPORT_TIME, then sends at most once per
    # Asia/Dhaka calendar date via its own idempotency key — same
    # self-throttling pattern as run-intraday-analysis above, chosen
    # specifically because TELEGRAM_ML_REPORT_TIMEZONE is independently
    # configurable from CELERY_TIMEZONE and Celery's crontab schedule in
    # this version has no per-entry timezone of its own.
    "send-ml-daily-report": {
        "task": "notifications.tasks.send_ml_daily_report",
        "schedule": 60.0,
    },
    # Check operational health frequently, but the task sends a Telegram
    # alert only for new/re-fired conditions and applies its own cooldown.
    "send-ops-alerts-to-admin": {
        "task": "notifications.tasks.send_ops_alerts_to_admin",
        "schedule": 300.0,
    },
    # Automatic daily append — no dashboard Fetch button required. The
    # task itself (run_scheduled_append) honors AUTO_DAILY_APPEND.
    "append-market-1005": {
        "task": "market.tasks.append_daily_bars",
        "schedule": crontab(hour=10, minute=5, day_of_week=_BD_WEEK),
    },
    # Telegram open/close notices — the task itself skips non-trading days
    # (holiday calendar) and no-ops if Telegram isn't configured.
    "market-open-telegram": {
        "task": "notifications.tasks.send_market_open_notification",
        "schedule": crontab(hour=10, minute=0, day_of_week=_BD_WEEK),
    },
    "append-market-1405": {
        "task": "market.tasks.append_daily_bars",
        "schedule": crontab(hour=14, minute=5, day_of_week=_BD_WEEK),
    },
    # After-close settlement: settle due next-day-close forecasts, retrain
    # the close-learn model if anything settled, generate new forecasts.
    # The task itself honors AUTO_CLOSE_LEARN.
    "close-learn-settlement-1445": {
        "task": "market.tasks.close_learn_settlement",
        "schedule": crontab(hour=14, minute=45, day_of_week=_BD_WEEK),
    },
    "market-close-telegram": {
        "task": "notifications.tasks.send_market_close_notification",
        "schedule": crontab(hour=14, minute=45, day_of_week=_BD_WEEK),
    },
    "send-daily-digest": {
        "task": "notifications.tasks.send_daily_digest",
        "schedule": crontab(hour=15, minute=0, day_of_week=_BD_WEEK),
    },
    # DSE trailing-P/E snapshot — display only, one bulk request/day.
    "sync-pe-ratios-1010": {
        "task": "market.tasks.sync_pe_ratios",
        "schedule": crontab(hour=10, minute=10, day_of_week=_BD_WEEK),
    },
    # ML Reliability Monitor: capture today's predictions, settle due
    # outcomes, assess both model families against rolling windows. Runs
    # after close-learn-settlement/digest so the day's AnalysisResult/
    # NextDayCloseForecast rows already exist to capture from.
    "assess-ml-reliability-1520": {
        "task": "market.tasks.assess_ml_reliability",
        "schedule": crontab(hour=15, minute=20, day_of_week=_BD_WEEK),
    },
    # Autonomous virtual trading ticks every minute, but its task only runs
    # between 10:00 and five minutes before the 14:30 exchange close and
    # self-throttles to AUTO_PAPER_TRADING_INTERVAL.
    "run-paper-trading": {
        "task": "market.tasks.run_paper_trading",
        "schedule": 60.0,
    },
    "finalize-paper-trading-1425": {
        "task": "market.tasks.finalize_paper_trading_day",
        "schedule": crontab(hour=14, minute=25, day_of_week=_BD_WEEK),
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

# Standalone forward-return model retrain — daily, at a fixed off-hours
# time (AUTO_ML_TRAINING_TIME, default 00:30, config/settings/base.py —
# validated there for format), well before the 10:00 Asia/Dhaka market
# open so it never collides with market hours or the end-of-day pipeline
# regardless of the day of week. Only added to the schedule at all when
# AUTO_ML_TRAINING is on — the task also self-checks the flag (defense in
# depth against a manual .delay() call), but leaving a disabled entry out
# of beat_schedule entirely means it never even logs a "skipped" tick.
from django.conf import settings  # noqa: E402

if getattr(settings, "AUTO_ML_TRAINING", True):
    _ml_hour, _ml_minute = (int(p) for p in getattr(settings, "AUTO_ML_TRAINING_TIME", "00:30").split(":", 1))
    app.conf.beat_schedule["train-ml-model-daily"] = {
        "task": "market.tasks.train_ml_model",
        "schedule": crontab(hour=_ml_hour, minute=_ml_minute),
    }
