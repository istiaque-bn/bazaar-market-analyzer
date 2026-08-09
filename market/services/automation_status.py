"""Everything the Admin automation-status panel shows, in one place —
read-only aggregation over TaskRun/MLModelVersion/settings/existing
services. No secrets are read or returned here (see the module-level
note in each field below); this is deliberately a thin composition over
market.services.autosync/ops_metrics/ops_alerts/exchange_config/
reliability_report/ml_training, not a second monitoring system.
"""
from __future__ import annotations

from django.conf import settings
from django.utils import timezone

from market.models import TaskRun, TaskStatus

# One row per task this panel reports "last successful"/"last failed" for.
_TRACKED_TASKS = {
    "fetch": "market.tasks.sync_live_market",
    "intraday_analysis": "market.tasks.run_intraday_analysis",
    "full_analysis": "market.tasks.run_full_analysis",
    "daily_append": "market.tasks.append_daily_bars",
    "forecast_settlement": "market.tasks.close_learn_settlement",
    "ml_training": "market.tasks.train_ml_model",
    "reliability_assessment": "market.tasks.assess_ml_reliability",
}


def _last_run(task_name: str, status: str | None = None):
    qs = TaskRun.objects.filter(task_name=task_name)
    if status is not None:
        qs = qs.filter(status=status)
    return qs.order_by("-started_at").first()


def _run_summary(run) -> dict | None:
    if run is None:
        return None
    return {
        "id": run.id,
        "status": run.status,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "error": run.error[:300] if run.error else "",
    }


def automation_status_snapshot(*, summary: dict | None = None, alerts: list[dict] | None = None) -> dict:
    from market.services.autosync import get_sync_status, is_market_hours
    from market.services.exchange_config import enabled_exchanges
    from market.services.ml_training import active_model_version
    from market.services.ops_metrics import ops_summary
    from market.services.ops_alerts import evaluate_alerts

    sync_status = get_sync_status()
    enabled = enabled_exchanges()

    last_successful = {}
    last_failed = {}
    stage_rows = []
    for label, task_name in _TRACKED_TASKS.items():
        success = _run_summary(_last_run(task_name, TaskStatus.SUCCESS))
        failure = _run_summary(_last_run(task_name, TaskStatus.FAILURE))
        last_successful[label] = success
        last_failed[label] = failure
        stage_rows.append({"label": label, "success": success, "failure": failure})

    current_tasks = [
        _run_summary(r) | {"task_name": r.task_name}
        for r in TaskRun.objects.filter(status=TaskStatus.STARTED).order_by("-started_at")[:10]
    ]
    recent_failures = [
        _run_summary(r) | {"task_name": r.task_name}
        for r in TaskRun.objects.filter(status=TaskStatus.FAILURE).order_by("-started_at")[:10]
    ]

    active_model = active_model_version("forward_return_rf", exchange_scope="combined")
    if active_model is None and enabled:
        # DSE-only (or CSE-only) deployments serve a per-exchange model
        # instead of the combined one — see market.services.ml_model.
        active_model = active_model_version("forward_return_rf", exchange_scope=enabled[0])

    # The Admin panel already calculates these expensive aggregates for its
    # own cards. Accepting them here avoids running the same DB-heavy work a
    # second time during one page request; callers outside that page retain
    # the original standalone behavior.
    if summary is None or alerts is None:
        try:
            summary = ops_summary()
            alerts = evaluate_alerts(summary)
        except Exception:
            summary = None
            alerts = []

    return {
        "generated_at": timezone.now().isoformat(),
        "automation_enabled": {
            "market_sync": getattr(settings, "AUTO_MARKET_SYNC", True),
            "intraday_analysis": getattr(settings, "AUTO_INTRADAY_ANALYSIS", True),
            "daily_append": getattr(settings, "AUTO_DAILY_APPEND", True),
            "analyze_after_append": getattr(settings, "AUTO_ANALYZE_AFTER_APPEND", True),
            "close_learn": getattr(settings, "AUTO_CLOSE_LEARN", True),
            "ml_training": getattr(settings, "AUTO_ML_TRAINING", True),
        },
        "ml_training_schedule": {
            "time": getattr(settings, "AUTO_ML_TRAINING_TIME", "00:30"),
        },
        "enabled_exchanges": enabled,
        "market_open": is_market_hours(),
        "sync_status": sync_status,
        "last_successful": last_successful,
        "last_failed": last_failed,
        "stage_rows": stage_rows,
        "current_tasks": current_tasks,
        "recent_failures": recent_failures,
        "active_model": (
            {
                "model_name": active_model.model_name,
                "version": active_model.version,
                "exchange_scope": active_model.exchange_scope,
                "status": active_model.status,
                "trained_at": active_model.trained_at.isoformat(),
            }
            if active_model
            else None
        ),
        "alerts": alerts,
        "task_backlog": next((a for a in alerts if a["key"] == "task_backlog"), None),
    }


def telegram_report_status() -> dict:
    """Everything the Admin Panel's Telegram ML Report section shows.
    Never returns the bot token or the raw chat id — only its masked
    display form (notifications.models.mask_recipient) and whatever's
    already stored on MlDailyReportDelivery rows (also masked/redacted
    at write time — see notifications.tasks.send_ml_daily_report)."""
    from notifications.models import MlDailyReportDelivery, MlDailyReportStatus, mask_recipient

    history = list(MlDailyReportDelivery.objects.order_by("-report_date")[:10])
    last_success = MlDailyReportDelivery.objects.filter(status=MlDailyReportStatus.SENT).order_by("-report_date").first()
    last_failure = MlDailyReportDelivery.objects.filter(status=MlDailyReportStatus.FAILED).order_by("-report_date").first()
    last_generated = MlDailyReportDelivery.objects.order_by("-generated_at").first()

    active_model = active_model_version_for_report()

    return {
        "enabled": getattr(settings, "TELEGRAM_ML_DAILY_REPORT", True),
        "configured": bool(getattr(settings, "TELEGRAM_BOT_TOKEN", "")) and bool(getattr(settings, "TELEGRAM_ADMIN_CHAT_ID", "")),
        "time": getattr(settings, "TELEGRAM_ML_REPORT_TIME", "17:00"),
        "timezone": getattr(settings, "TELEGRAM_ML_REPORT_TIMEZONE", "Asia/Dhaka"),
        "recipient_masked": mask_recipient(getattr(settings, "TELEGRAM_ADMIN_CHAT_ID", "")),
        "last_generated": last_generated,
        "last_success": last_success,
        "last_failure": last_failure,
        "history": history,
        "active_model_summary": active_model,
    }


def active_model_version_for_report() -> str:
    from market.services.ml_daily_report import FORWARD_MODEL_NAME, _resolve_scope

    _scope, model = _resolve_scope(FORWARD_MODEL_NAME)
    if model is None:
        return "No active model"
    return f"{model.model_name}[{model.exchange_scope}] v{model.version} ({model.status})"
