"""Feedback notifications — reuses the existing notifications.models.Alert
system exactly like notifications/tasks.py's send_daily_digest does
(in-app Alert row always; Telegram/email only if the reporter opted in
AND that channel is actually configured). No second notification system.
"""
from __future__ import annotations

import requests
from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.db.utils import OperationalError
from django.utils import timezone

from market.services.task_status import record_task_run
from notifications.models import Alert, AlertChannel
from notifications.services import send_telegram_message

# "Material" events only — matches the spec's "notify on received /
# clarification requested / status changes materially / public response
# / implemented-or-resolved" and explicit "do not notify for every
# internal-note edit". note_added/priority_set/assigned never reach here
# (feedback.services never calls notify_reporter for them).
_EVENT_TITLES = {
    "received": "We received your feedback",
    "status_changed": "Your feedback status changed",
    "response_posted": "Admin responded to your feedback",
}


@shared_task(
    name="feedback.tasks.notify_reporter",
    autoretry_for=(TimeoutError, OperationalError, requests.exceptions.RequestException),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
    time_limit=60,
    soft_time_limit=45,
)
@record_task_run("feedback.tasks.notify_reporter")
def notify_reporter(feedback_id: int, event: str):
    from feedback.models import Feedback

    if event not in _EVENT_TITLES:
        return {"ok": True, "skipped": f"unrecognized event {event!r}"}

    fb = Feedback.objects.select_related("reporter", "reporter__profile").filter(id=feedback_id).first()
    if fb is None or fb.reporter is None:
        # Reporter account deleted since — reference_username_snapshot
        # keeps the record legible, but there's genuinely no one left to
        # notify.
        return {"ok": True, "skipped": "no reporter"}

    title = f"[{fb.reference_number}] {_EVENT_TITLES[event]}"
    message_lines = [f"{fb.title}", f"Status: {fb.get_status_display()}"]
    if event == "response_posted" and fb.admin_response:
        message_lines.append(f"Response: {fb.admin_response}")
    message = "\n".join(message_lines)

    Alert.objects.create(
        user=fb.reporter,
        channel=AlertChannel.IN_APP,
        title=title,
        message=message,
        is_sent=True,
        sent_at=timezone.now(),
    )

    profile = getattr(fb.reporter, "profile", None)
    sent = {"telegram": False, "email": False}
    if profile and profile.telegram_alerts and profile.telegram_chat_id:
        sent["telegram"] = send_telegram_message(profile.telegram_chat_id, f"{title}\n\n{message}")
    if profile and profile.email_alerts and fb.reporter.email and settings.EMAIL_HOST:
        send_mail(
            subject=title,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[fb.reporter.email],
            fail_silently=True,
        )
        sent["email"] = True
    return {"ok": True, "in_app": True, **sent}
