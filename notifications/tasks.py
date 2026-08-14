import hashlib
from datetime import date, timedelta
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
from market.models import AnalysisResult, Watchlist
from market.services.screener import potential_shares, safe_buys, screen_summary, sell_candidates
from market.services.signal_status import market_edge_status
from market.services.task_status import record_task_run
from notifications.models import Alert, AlertChannel, AlertRule, AlertRuleType, AdminReminder, MlDailyReportDelivery, MlDailyReportStatus, mask_recipient
from notifications.services import TelegramPermanentError, TelegramTransientError, send_telegram_message, send_telegram_message_tracked


@shared_task(name="notifications.tasks.deliver_admin_reminders")
@record_task_run("notifications.tasks.deliver_admin_reminders")
def deliver_admin_reminders():
    due = AdminReminder.objects.filter(remind_on__lte=timezone.localdate(), delivered_at__isnull=True).select_related("admin")
    delivered = 0
    for reminder in due:
        # Re-check before delivery. In normal operation there is one beat
        # process, and delivered_at makes retries harmless; this extra
        # lookup also protects a stale queryset from a concurrent worker.
        if not AdminReminder.objects.filter(pk=reminder.pk, delivered_at__isnull=True).exists():
            continue
        text = f"Bazaar reminder for {reminder.remind_on}:\n{reminder.action}"
        now = timezone.now()
        Alert.objects.create(
            user=reminder.admin,
            channel=AlertChannel.IN_APP,
            title="Admin reminder",
            message=text,
            is_sent=True,
            sent_at=now,
        )
        if reminder.telegram_enabled:
            send_telegram_message(getattr(settings, "TELEGRAM_ADMIN_CHAT_ID", ""), text)
        # ``email_enabled`` records the admin's future preference only.
        # Reminder emails intentionally remain disabled until the separate
        # email-delivery feature is approved and implemented.
        AdminReminder.objects.filter(pk=reminder.pk, delivered_at__isnull=True).update(delivered_at=now)
        delivered += 1
    return {"ok": True, "delivered": delivered}


PERSONAL_ALERT_COOLDOWN = timedelta(hours=4)


def _rule_trigger(rule, analysis):
    """Return a safe, human-readable trigger message, or None.

    Signal rules establish a baseline before alerting; this prevents a
    newly-created rule from sending an old signal as if it just changed.
    """
    stock = rule.stock
    if rule.rule_type == AlertRuleType.TARGET_PRICE and rule.target_price is not None and stock.last_price is not None:
        if float(stock.last_price) >= float(rule.target_price):
            return f"{stock.trading_code} reached BDT {stock.last_price:.2f} (your target: {rule.target_price})."
    elif rule.rule_type == AlertRuleType.PERCENT_MOVE and rule.threshold_pct is not None and stock.last_change_pct is not None:
        if abs(float(stock.last_change_pct)) >= float(rule.threshold_pct):
            return f"{stock.trading_code} moved {stock.last_change_pct:+.2f}% today (your threshold: {rule.threshold_pct}%)."
    elif rule.rule_type == AlertRuleType.SIGNAL_CHANGE and analysis:
        current = analysis.action
        if not rule.last_signal:
            rule.last_signal = current
            rule.save(update_fields=["last_signal", "updated_at"])
        elif current != rule.last_signal:
            old = rule.last_signal
            rule.last_signal = current
            rule.save(update_fields=["last_signal", "updated_at"])
            return f"{stock.trading_code} signal changed from {old} to {current} (score {analysis.score:.0f})."
    elif rule.rule_type == AlertRuleType.CONFIDENCE_CHANGE and analysis and rule.min_confidence is not None:
        confidence = float(analysis.confidence or 0) * 100
        if confidence >= float(rule.min_confidence):
            rule.last_confidence = confidence
            rule.save(update_fields=["last_confidence", "updated_at"])
            return f"{stock.trading_code} prediction confidence is {confidence:.0f}% (your threshold: {rule.min_confidence}%)."
    return None


@shared_task(name="notifications.tasks.evaluate_personal_alert_rules")
@record_task_run("notifications.tasks.evaluate_personal_alert_rules")
def evaluate_personal_alert_rules():
    """Evaluate user rules from persisted data, then deliver in-app and/or Telegram.

    A four-hour cooldown applies per rule. Telegram is sent only where the
    user enabled both the profile-wide consent and the individual rule.
    """
    now = timezone.now()
    delivered = 0
    rules = AlertRule.objects.filter(is_active=True).select_related("stock", "user", "user__profile")
    for rule in rules:
        if rule.last_triggered_at and now - rule.last_triggered_at < PERSONAL_ALERT_COOLDOWN:
            continue
        analysis = rule.stock.analyses.order_by("-as_of").first()
        message = _rule_trigger(rule, analysis)
        if not message:
            continue
        title = f"Alert: {rule.stock.trading_code}"
        if rule.in_app_enabled:
            Alert.objects.create(user=rule.user, stock=rule.stock, analysis=analysis, rule=rule, channel=AlertChannel.IN_APP, title=title, message=message, is_sent=True, sent_at=now)
        profile = getattr(rule.user, "profile", None)
        if rule.telegram_enabled and profile and profile.telegram_alerts and profile.telegram_chat_id:
            sent = send_telegram_message(profile.telegram_chat_id, f"{title}\n{message}\n\n{RESEARCH_DISCLAIMER}")
            if sent:
                Alert.objects.create(user=rule.user, stock=rule.stock, analysis=analysis, rule=rule, channel=AlertChannel.TELEGRAM, title=title, message=message, is_sent=True, sent_at=now)
        rule.last_triggered_at = now
        rule.save(update_fields=["last_triggered_at", "updated_at"])
        delivered += 1
    return {"ok": True, "triggered_rules": delivered}


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


def _personal_watchlist_digest_text(user, market_digest: str | None = None) -> str:
    """Add a concise, user-specific watchlist section to the daily digest.

    The market digest remains the shared in-app record.  Delivery channels
    receive this version so a user's Telegram/email message answers the more
    useful question: what changed in *their* saved shares?  It uses only the
    latest persisted analysis, never a fresh quote or prediction request.
    """
    watchlist = Watchlist.objects.filter(user=user, name="Default").first()
    stocks = list(watchlist.stocks.all().order_by("trading_code")) if watchlist else []
    if not stocks:
        return (market_digest or _digest_text()) + "\n\nYour watchlist\nNo shares saved yet. Add shares from a stock page to receive a personal summary."

    latest = {}
    for analysis in AnalysisResult.objects.filter(stock__in=stocks).order_by("stock_id", "-as_of"):
        latest.setdefault(analysis.stock_id, analysis)

    action_counts = {"BUY": 0, "SELL": 0, "WATCH": 0, "HOLD": 0, "UNAVAILABLE": 0}
    for stock in stocks:
        action = latest.get(stock.id).action if stock.id in latest else "UNAVAILABLE"
        action_counts[action] = action_counts.get(action, 0) + 1

    counts = " · ".join(
        f"{label.title()} {count}"
        for label, count in action_counts.items()
        if count
    )
    lines = [market_digest or _digest_text(), "", f"Your watchlist ({len(stocks)} shares)", counts]
    for stock in stocks:
        analysis = latest.get(stock.id)
        if not analysis:
            lines.append(f"• {stock.trading_code} ({stock.exchange}): analysis is not available yet.")
            continue
        rationale = (analysis.rationale or "No additional explanation is available.").replace("\n", " ").strip()
        lines.append(
            f"• {stock.trading_code} ({stock.exchange}): {analysis.action} "
            f"(score {analysis.score:.0f}, confidence {float(analysis.confidence or 0):.0%}) — {rationale[:180]}"
        )
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
        personal_text = _personal_watchlist_digest_text(user, text)
        if profile and profile.telegram_alerts and profile.telegram_chat_id:
            send_telegram_message(profile.telegram_chat_id, personal_text)
        if profile and profile.email_alerts and user.email:
            send_mail(
                subject=f"[Bazaar] Daily market digest {timezone.localdate()}",
                message=personal_text,
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


# ---------------------------------------------------------------------------
# Telegram operational alerts
# ---------------------------------------------------------------------------


@shared_task(
    bind=True,
    name="notifications.tasks.send_ops_alerts_to_admin",
    autoretry_for=(TelegramTransientError,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=5,
    time_limit=90,
    soft_time_limit=60,
)
@record_task_run("notifications.tasks.send_ops_alerts_to_admin")
def send_ops_alerts_to_admin(self):
    """Send newly-firing operational alerts to the private admin Telegram
    chat. An alert must remain absent for the cooldown window before it can
    notify again, so a continuing outage is visible without becoming spam."""
    from datetime import timedelta

    from market.services.locking import LockBusy, distributed_lock
    from market.services.ops_alerts import evaluate_alerts
    from market.models import TaskAlertState, TaskHealth

    if not getattr(settings, "TELEGRAM_OPS_ALERTS", True):
        return {"ok": True, "skipped": "disabled"}
    if not (settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_ADMIN_CHAT_ID):
        return {"ok": True, "skipped": "not_configured"}

    try:
        with distributed_lock("telegram-ops-alerts", timeout=90, blocking_timeout=0):
            cooldown_start = timezone.now() - timedelta(minutes=settings.TELEGRAM_OPS_ALERT_COOLDOWN_MINUTES)
            fresh = []
            health_notifications = []
            # These are durable transitions, not a repeated query over old
            # TaskRun rows. A scan may run every five minutes without paging
            # again until recovery (or a future explicit escalation).
            for health in TaskHealth.objects.filter(alert_state__in=[TaskAlertState.FAILURE_PENDING, TaskAlertState.RECOVERY_PENDING]):
                if health.alert_state == TaskAlertState.FAILURE_PENDING:
                    health_notifications.append((health, {
                        "key": f"task_unhealthy_{health.task_name}", "severity": "critical",
                        "message": f"{health.task_name}: UNHEALTHY after {health.consecutive_failures} consecutive failures. {health.last_error[:200]}",
                    }))
                else:
                    health_notifications.append((health, {
                        "key": f"task_recovered_{health.task_name}", "severity": "warning",
                        "message": f"{health.task_name}: recovered; failure counter reset after a successful run.",
                    }))
            for alert in evaluate_alerts():
                if alert["key"].startswith("repeated_failure_"):
                    continue
                title = f"Ops alert: {alert['key']}"
                already_notified = Alert.objects.filter(
                    user__isnull=True,
                    channel=AlertChannel.TELEGRAM,
                    title=title,
                    created_at__gte=cooldown_start,
                ).exists()
                if not already_notified:
                    fresh.append(alert)

            fresh.extend(alert for _, alert in health_notifications)

            if not fresh:
                return {"ok": True, "skipped": "no_new_alerts"}

            lines = ["⚠️ Bazaar operational alert"]
            for alert in fresh[:10]:
                lines.append(f"• {alert['severity'].upper()}: {alert['message']}")
            if len(fresh) > 10:
                lines.append(f"• Plus {len(fresh) - 10} more alerts — open /ops/ for details.")
            text = "\n".join(lines)
            send_telegram_message_tracked(settings.TELEGRAM_ADMIN_CHAT_ID, text)

            now = timezone.now()
            Alert.objects.bulk_create(
                [
                    Alert(
                        user=None,
                        channel=AlertChannel.TELEGRAM,
                        title=f"Ops alert: {alert['key']}",
                        message=alert["message"],
                        is_sent=True,
                        sent_at=now,
                    )
                    for alert in fresh
                ]
            )
            for health, _ in health_notifications:
                health.last_alert = now
                health.alert_state = (
                    TaskAlertState.FAILURE_SENT
                    if health.current_health_status == "unhealthy"
                    else TaskAlertState.NONE
                )
                health.save(update_fields=["last_alert", "alert_state"])
            return {"ok": True, "sent": len(fresh)}
    except LockBusy:
        return {"ok": True, "skipped": "already_running"}


# ---------------------------------------------------------------------------
# Telegram market open/close notices
# ---------------------------------------------------------------------------


def _market_session_text(is_open: bool, as_of: date) -> str:
    from market.services.exchange_config import enabled_exchanges

    exchanges = " & ".join(enabled_exchanges()) or "Market"
    if is_open:
        return f"🟢 {exchanges} market is now OPEN — {as_of.isoformat()}, 10:00 Asia/Dhaka"
    return f"🔴 {exchanges} market is now CLOSED — {as_of.isoformat()}, 14:45 Asia/Dhaka"


def _send_market_session_notification(is_open: bool) -> dict:
    """Shared body for the open/close notice tasks below. Same idempotency
    shape as send_daily_digest: a per-day Alert(user=None, title=...) row
    doubles as both the in-app audit record and the duplicate-send guard,
    inside a lock so a retry/double beat-fire can't race past the check."""
    from market.services.locking import LockBusy, distributed_lock
    from market.services.market_hours import TRADING_WEEKDAYS
    from market.services.trading_calendar import closure_reason

    today = timezone.localdate()
    if today.weekday() not in TRADING_WEEKDAYS or closure_reason(today) is not None:
        return {"ok": True, "skipped": "non_trading_day"}
    if not (settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_ADMIN_CHAT_ID):
        return {"ok": True, "skipped": "not_configured"}

    kind = "open" if is_open else "close"
    title = f"Market {kind} {today.isoformat()}"
    try:
        with distributed_lock(f"market-{kind}-notice:{today.isoformat()}", timeout=60, blocking_timeout=0):
            if Alert.objects.filter(user__isnull=True, title=title).exists():
                return {"ok": True, "skipped": "already_sent"}
            text = _market_session_text(is_open, today)
            sent = send_telegram_message(settings.TELEGRAM_ADMIN_CHAT_ID, text)
            Alert.objects.create(
                user=None,
                channel=AlertChannel.IN_APP,
                title=title,
                message=text,
                is_sent=True,
                sent_at=timezone.now(),
            )
            return {"ok": True, "sent": sent}
    except LockBusy:
        return {"ok": True, "skipped": "already_running"}


@shared_task(
    name="notifications.tasks.send_market_open_notification",
    autoretry_for=(TimeoutError, OperationalError, requests.exceptions.RequestException),
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=3,
    time_limit=60,
    soft_time_limit=45,
)
@record_task_run("notifications.tasks.send_market_open_notification")
def send_market_open_notification():
    return _send_market_session_notification(is_open=True)


@shared_task(
    name="notifications.tasks.send_market_close_notification",
    autoretry_for=(TimeoutError, OperationalError, requests.exceptions.RequestException),
    retry_backoff=True,
    retry_backoff_max=120,
    retry_jitter=True,
    max_retries=3,
    time_limit=60,
    soft_time_limit=45,
)
@record_task_run("notifications.tasks.send_market_close_notification")
def send_market_close_notification():
    return _send_market_session_notification(is_open=False)
