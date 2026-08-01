import requests
from celery import shared_task
from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.db.utils import OperationalError
from django.utils import timezone

from market.services.predictor import RESEARCH_DISCLAIMER
from market.services.screener import potential_shares, safe_buys, screen_summary, sell_candidates
from market.services.signal_status import market_edge_status
from market.services.task_status import record_task_run
from notifications.models import Alert, AlertChannel
from notifications.services import send_telegram_message


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
