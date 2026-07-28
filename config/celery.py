import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("bazaar")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Celery crontab: 0=Sunday … 6=Saturday (like cron)
# Bangladesh market week: Sunday–Thursday
_BD_WEEK = "0-4"

app.conf.beat_schedule = {
    # Automatic daily append — no dashboard Fetch button required
    "append-market-1005": {
        "task": "market.tasks.append_daily_bars",
        "schedule": crontab(hour=10, minute=5, day_of_week=_BD_WEEK),
    },
    "append-market-1405": {
        "task": "market.tasks.append_daily_bars",
        "schedule": crontab(hour=14, minute=5, day_of_week=_BD_WEEK),
    },
    "send-daily-digest": {
        "task": "notifications.tasks.send_daily_digest",
        "schedule": crontab(hour=15, minute=0, day_of_week=_BD_WEEK),
    },
}
