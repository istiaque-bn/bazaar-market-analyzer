import hashlib
from datetime import date
from zoneinfo import ZoneInfo

import requests
from celery import shared_task
from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.db.utils import OperationalError
from django.utils import timezone

from market.services.ml_daily_report import COMPARISON_TOLERANCE_PCT, build_report_context, render_report_sections, split_for_telegram
from market.services.predictor import RESEARCH_DISCLAIMER
from market.services.screener import potential_shares, safe_buys, screen_summary, sell_candidates
from market.services.signal_status import market_edge_status
from market.services.task_status import record_task_run
from notifications.models import Alert, AlertChannel, MlDailyReportDelivery, MlDailyReportStatus, mask_recipient
from notifications.services import TelegramPermanentError, TelegramTransientError, send_telegram_message, send_telegram_message_tracked


def _digest_text() -> str:
    summary = screen_summary()
    try:
        edge = market_edge_status()
    except Exception:
        edge = {"has_edge": False, "edge_reason": "Model status unavailable."}
    lines = [
        f"Bazaar Daily Digest — {summary['as_of']}",
        RESEARCH_DISCLAIMER,
        "",
        f"Model status: {'demonstrated edge' if edge['has_edge'] else 'NO demonstrated predictive edge'} — {edge['edge_reason']}",
        "",
        f"Signals: BUY {summary['buy']} | SELL {summary['sell']} | WATCH {summary['watch']} | HOLD {summary['hold']}",
        f"Research candidates: {summary['research_candidates']}",
        "",
        "Top potential shares:",
    ]
    for r in potential_shares(8):
        lines.append(
            f"• {r.stock.trading_code} ({r.stock.exchange}) score={r.score:.0f} "
            f"mature~{r.maturity_days_est}d peak~{r.peak_days_est}d conf={r.confidence:.0%}"
        )
    lines.append("")
    if edge["has_edge"]:
        lines.append("Experimental research candidates (not a recommendation):")
    else:
        lines.append("Experimental research candidates (no demonstrated predictive edge right now — treat as informational only):")
    for r in safe_buys(5):
        lines.append(f"• {r.stock.trading_code}: {r.rationale[:120]}")
    lines.append("")
    lines.append("Sell / caution:")
    for r in sell_candidates(5):
        lines.append(f"• {r.stock.trading_code} score={r.score:.0f}")
    lines.append("")
    lines.append("Estimates are probabilistic from ~1y history — not financial advice.")
    return "\n".join(lines)


@shared_task(
    name="notifications.tasks.send_daily_digest",
    autoretry_for=(TimeoutError, OperationalError, requests.exceptions.RequestException),
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=3,
    time_limit=120,
    soft_time_limit=90,
)
@record_task_run("notifications.tasks.send_daily_digest")
def send_daily_digest():
    from market.services.locking import LockBusy, distributed_lock

    title = f"Daily digest {timezone.localdate()}"
    try:
        # Own lock (not the market-write one): serializes duplicate digest
        # triggers against each other without blocking unrelated
        # market-writing tasks, and covers the whole send so the
        # idempotency check below can't race across workers.
        with distributed_lock("daily-digest", timeout=90, blocking_timeout=30):
            # Idempotency: a duplicate trigger (retry, manual re-run, a
            # double beat fire) for the same day must not re-send.
            if Alert.objects.filter(user__isnull=True, title=title).exists():
                return {"skipped": "already sent today"}

            text = _digest_text()
            sent = {"telegram": False, "email": 0, "in_app": 0}

            if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID:
                sent["telegram"] = send_telegram_message(settings.TELEGRAM_CHAT_ID, text)

            Alert.objects.create(
                user=None,
                channel=AlertChannel.IN_APP,
                title=title,
                message=text,
                is_sent=True,
                sent_at=timezone.now(),
            )
            sent["in_app"] = 1
    except LockBusy:
        return {"skipped": "another worker is already sending today's digest"}

    for user in User.objects.filter(is_active=True).select_related("profile"):
        # No per-user in-app Alert here: the global (user=None) row above already
        # matches every user via the Q(user=request.user) | Q(user__isnull=True)
        # filter used in views, so a second row would just duplicate it.
        profile = getattr(user, "profile", None)
        if profile and profile.telegram_alerts and profile.telegram_chat_id:
            send_telegram_message(profile.telegram_chat_id, text)
        if profile and profile.email_alerts and user.email:
            send_mail(
                subject=f"[Bazaar] Daily market digest {timezone.localdate()}",
                message=text,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                fail_silently=True,
            )
            sent["email"] += 1
    return sent


# ---------------------------------------------------------------------------
# Telegram ML daily report
# ---------------------------------------------------------------------------


def _report_date_in_configured_tz() -> date:
    tz = ZoneInfo(settings.TELEGRAM_ML_REPORT_TIMEZONE)
    return timezone.now().astimezone(tz).date()


def _recipient_ref(chat_id: str) -> str:
    return hashlib.sha256((chat_id or "unset").encode()).hexdigest()[:16]


def _idempotency_key(report_date: date, chat_id: str) -> str:
    return f"telegram_ml_daily_report:{_recipient_ref(chat_id)}:{report_date.isoformat()}"


def _snapshot_for_comparison(context: dict) -> dict:
    return {
        "model_version_tag": context["model_version_tag"],
        "window_label": context["window_label"],
        "live_n": context["live"]["n"],
        "live_precision": context["live"]["precision"],
        "status_label": context["status_label"],
    }


def _compare_with_previous(report_date: date, context: dict) -> dict | None:
    """"What changed?" — compares today's context against the most recent
    successfully SENT report's stored snapshot. Never compares across a
    model-version or evaluation-window change without saying so; a
    zero-movement day (no new settled predictions, no new training,
    same version) is reported as "no new evidence", not as an error or
    a fabricated "stable" verdict."""
    previous = (
        MlDailyReportDelivery.objects.filter(report_date__lt=report_date, status=MlDailyReportStatus.SENT)
        .order_by("-report_date")
        .first()
    )
    if previous is None:
        return None
    prev = (previous.detail or {}).get("snapshot") or {}
    if not prev:
        return None

    if prev.get("model_version_tag") != context["model_version_tag"]:
        return {"message": "A new model version was introduced, so today's result is not directly comparable with yesterday's."}
    if prev.get("window_label") != context["window_label"]:
        return {"message": "The evaluation window changed, so today's result is not directly comparable with yesterday's."}

    cur_live_n = context["live"]["n"]
    if cur_live_n == prev.get("live_n") and not context["trained_today"]:
        return {"message": "No new evidence since the previous report."}

    prev_precision, cur_precision = prev.get("live_precision"), context["live"]["precision"]
    if prev_precision is None or cur_precision is None:
        return {"message": "New live evidence arrived; not enough history yet to say whether it improved or declined."}

    diff_pct = (cur_precision - prev_precision) * 100
    if abs(diff_pct) < COMPARISON_TOLERANCE_PCT:
        return {"message": "Performance is stable compared with the previous report."}
    if diff_pct > 0:
        return {"message": "Performance improved slightly compared with the previous report."}
    return {"message": "Performance declined compared with the previous report."}


@shared_task(
    bind=True,
    name="notifications.tasks.send_ml_daily_report",
    autoretry_for=(TelegramTransientError,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=5,
    time_limit=120,
    soft_time_limit=90,
)
@record_task_run("notifications.tasks.send_ml_daily_report")
def send_ml_daily_report(self, force: bool = False, manual: bool = False):
    """One consolidated, plain-language ML report to the configured Admin
    Telegram recipient, at most once per (recipient, Asia/Dhaka-or-
    configured-timezone calendar date) — see market.services.ml_daily_report
    for the deterministic content and notifications.models.MlDailyReportDelivery
    for the delivery/idempotency record.

    `manual` bypasses the "not yet time" self-throttle (an admin clicking
    "Send now" means now, not 17:00) but still respects the
    already-sent-today idempotency guard unless `force` is also set —
    force is for an explicit, audited resend of a confirmed duplicate
    (see market/views.py's Telegram report admin actions)."""
    from market.services.locking import LockBusy, distributed_lock

    if not settings.TELEGRAM_ML_DAILY_REPORT:
        return {"ok": True, "skipped": "disabled"}

    report_date = _report_date_in_configured_tz()
    if not manual:
        tz = ZoneInfo(settings.TELEGRAM_ML_REPORT_TIMEZONE)
        now_local = timezone.now().astimezone(tz)
        try:
            target_hour, target_minute = (int(p) for p in settings.TELEGRAM_ML_REPORT_TIME.split(":", 1))
        except ValueError:
            target_hour, target_minute = 17, 0
        if (now_local.hour, now_local.minute) < (target_hour, target_minute):
            return {"ok": True, "skipped": "not_yet_time"}

    chat_id = settings.TELEGRAM_ADMIN_CHAT_ID
    idempotency_key = _idempotency_key(report_date, chat_id)

    try:
        with distributed_lock(f"ml-daily-report:{idempotency_key}", timeout=180, blocking_timeout=0):
            delivery, _created = MlDailyReportDelivery.objects.get_or_create(
                idempotency_key=idempotency_key,
                defaults={"report_date": report_date, "recipient_masked": mask_recipient(chat_id)},
            )

            if delivery.status == MlDailyReportStatus.SENT and not force:
                return {"ok": True, "skipped": "already_sent"}

            context = build_report_context(as_of=report_date)
            comparison = _compare_with_previous(report_date, context)
            sections = render_report_sections(context, comparison=comparison)
            chunks = split_for_telegram(sections)
            full_text = "\n\n".join(sections)

            detail = dict(delivery.detail or {})
            detail["snapshot"] = _snapshot_for_comparison(context)
            detail["chunks_total"] = len(chunks)
            if force:
                detail["chunks_sent"] = 0
                detail["message_ids"] = []
            detail.setdefault("chunks_sent", 0)
            detail.setdefault("message_ids", [])

            delivery.report_text = full_text
            delivery.content_hash = hashlib.sha256(full_text.encode()).hexdigest()[:16]
            delivery.model_version_summary = context["model_version_tag"] or "no active model"
            delivery.detail = detail

            if not (settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_ADMIN_CHAT_ID):
                delivery.status = MlDailyReportStatus.SKIPPED
                delivery.last_error = "Telegram bot token or admin chat id is not configured."
                delivery.save()
                return {"ok": True, "skipped": "not_configured"}

            already_sent = detail["chunks_sent"]
            message_ids = detail["message_ids"]
            try:
                for i, chunk in enumerate(chunks):
                    if i < already_sent:
                        continue
                    result = send_telegram_message_tracked(chat_id, chunk)
                    message_ids.append(result.get("message_id"))
                    delivery.attempt_count += 1
                    detail["chunks_sent"] = i + 1
                    detail["message_ids"] = message_ids
                    delivery.detail = detail
                    delivery.save()
            except TelegramTransientError as exc:
                delivery.status = (
                    MlDailyReportStatus.FAILED if self.request.retries >= self.max_retries else MlDailyReportStatus.RETRYING
                )
                delivery.last_error = str(exc)[:1000]
                delivery.save()
                raise
            except TelegramPermanentError as exc:
                delivery.status = MlDailyReportStatus.FAILED
                delivery.last_error = str(exc)[:1000]
                delivery.save()
                return {"ok": False, "error": "telegram_permanent_error"}

            delivery.status = MlDailyReportStatus.SENT
            delivery.sent_at = timezone.now()
            delivery.telegram_message_id = str(message_ids[0]) if message_ids and message_ids[0] else ""
            delivery.save()
            return {"ok": True, "sent": True, "chunks": len(chunks)}
    except LockBusy:
        return {"ok": True, "skipped": "already_running"}
