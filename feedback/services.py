"""Feedback business logic: creation, role-gated status/priority/
assignment transitions, and the admin dashboard summary. Views call
these instead of touching Feedback rows directly, so the audit trail
(FeedbackEvent) and role rules can't be bypassed by a new call site
forgetting to add them — same design as accounts/services.py.
"""
from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from accounts.roles import is_admin, is_staff_member
from feedback.models import (
    CLOSED_STATUSES,
    DIAGNOSTIC_CATEGORIES,
    STAFF_ALLOWED_STATUSES,
    USER_ALLOWED_STATUSES,
    Feedback,
    FeedbackEvent,
    FeedbackEventType,
    FeedbackStatus,
)


class FeedbackActionError(Exception):
    """A rule violation the caller should show to the user, not a 500."""


def _record_event(feedback: Feedback, actor, event_type: str, detail: dict | None = None) -> FeedbackEvent:
    return FeedbackEvent.objects.create(
        feedback=feedback,
        actor=actor if (actor is not None and getattr(actor, "is_authenticated", False)) else None,
        actor_username_snapshot=actor.get_username() if (actor and getattr(actor, "is_authenticated", False)) else "",
        event_type=event_type,
        detail=detail or {},
    )


def capture_diagnostic_metadata(category: str, exchange: str, trading_code: str) -> dict:
    """For Prediction Issue / Data Issue feedback: look up *real*
    server-side records for the exchange/trading_code the reporter named
    and pull safe, non-sensitive fields from them. Deliberately ignores
    any other diagnostic value the client might submit (model version,
    quote timestamp, data source, prediction reference) — those are
    never trusted from a hidden browser field, only ever derived here
    from what the database actually has on record right now."""
    empty = {
        "meta_exchange": "",
        "meta_trading_code": "",
        "meta_analysis_date": None,
        "meta_model_version": "",
        "meta_quote_timestamp": None,
        "meta_data_source": "",
        "meta_prediction_reference": "",
    }
    if category not in DIAGNOSTIC_CATEGORIES or not exchange or not trading_code:
        return empty

    from market.models import AnalysisResult, PriceHistory, Stock

    stock = Stock.objects.filter(exchange=exchange.upper(), trading_code=trading_code.upper()).first()
    if stock is None:
        return empty

    analysis = AnalysisResult.objects.filter(stock=stock).order_by("-as_of").first()
    latest_price = PriceHistory.objects.filter(stock=stock).order_by("-date").first()

    return {
        "meta_exchange": stock.exchange,
        "meta_trading_code": stock.trading_code,
        "meta_analysis_date": analysis.as_of if analysis else None,
        "meta_model_version": ((analysis.features or {}).get("model_version") or "") if analysis else "",
        "meta_quote_timestamp": stock.updated_at,
        "meta_data_source": latest_price.get_source_display() if latest_price else "",
        "meta_prediction_reference": f"{stock.exchange}:{stock.trading_code}:{analysis.as_of}" if analysis else "",
    }


@transaction.atomic
def create_feedback(reporter, *, category, title, description, reporter_priority, page_path="", steps_to_reproduce="",
                     expected_behavior="", actual_behavior="", contact_allowed=True, meta_exchange="", meta_trading_code="") -> Feedback:
    diagnostic = capture_diagnostic_metadata(category, meta_exchange, meta_trading_code)
    fb = Feedback.objects.create(
        reporter=reporter,
        reporter_username_snapshot=reporter.get_username(),
        category=category,
        title=title,
        description=description,
        reporter_priority=reporter_priority,
        page_path=page_path[:255],
        steps_to_reproduce=steps_to_reproduce,
        expected_behavior=expected_behavior,
        actual_behavior=actual_behavior,
        contact_allowed=contact_allowed,
        **diagnostic,
    )
    _record_event(fb, reporter, FeedbackEventType.CREATED, {"category": category})
    from feedback.tasks import notify_reporter

    notify_reporter.delay(fb.id, "received")
    return fb


def _actor_role(actor) -> str:
    if is_admin(actor):
        return "admin"
    if is_staff_member(actor):
        return "staff"
    return "user"


def set_status(feedback: Feedback, actor, new_status: str, note: str = "") -> Feedback:
    if new_status not in FeedbackStatus.values:
        raise FeedbackActionError("Unknown status.")
    role = _actor_role(actor)
    if role == "user":
        if feedback.reporter_id != actor.id:
            raise PermissionDenied("You can only withdraw your own feedback.")
        if new_status not in USER_ALLOWED_STATUSES:
            raise PermissionDenied("Users may only withdraw their own feedback.")
        if feedback.is_closed:
            raise FeedbackActionError("This item is already closed.")
    elif role == "staff":
        if new_status not in STAFF_ALLOWED_STATUSES:
            raise PermissionDenied("Staff may not set this status — it requires Admin.")
    # Admin: any status is allowed.

    old_status = feedback.status
    if old_status == new_status:
        return feedback
    feedback.status = new_status
    if new_status != FeedbackStatus.NEW and feedback.reviewed_at is None:
        feedback.reviewed_at = timezone.now()
    if new_status in CLOSED_STATUSES and feedback.resolved_at is None:
        feedback.resolved_at = timezone.now()
    feedback.save(update_fields=["status", "reviewed_at", "resolved_at", "updated_at"])
    _record_event(feedback, actor, FeedbackEventType.STATUS_CHANGED, {"from": old_status, "to": new_status, "note": note[:500]})

    from feedback.tasks import notify_reporter

    notify_reporter.delay(feedback.id, "status_changed")
    return feedback


def set_admin_priority(feedback: Feedback, actor, priority: str) -> Feedback:
    if not is_admin(actor):
        raise PermissionDenied("Only Admin can set the final priority.")
    feedback.admin_priority = priority
    feedback.save(update_fields=["admin_priority", "updated_at"])
    _record_event(feedback, actor, FeedbackEventType.PRIORITY_SET, {"priority": priority})
    return feedback


def assign(feedback: Feedback, actor, assignee) -> Feedback:
    role = _actor_role(actor)
    if role == "user":
        raise PermissionDenied("Users cannot assign feedback.")
    if role == "staff" and assignee.id != actor.id:
        raise PermissionDenied("Staff can only assign feedback to themselves.")
    if not (assignee.is_staff or assignee.is_superuser):
        raise FeedbackActionError("Feedback can only be assigned to a Staff or Admin account.")
    feedback.assigned_to = assignee
    feedback.save(update_fields=["assigned_to", "updated_at"])
    _record_event(feedback, actor, FeedbackEventType.ASSIGNED, {"assignee": assignee.username})
    return feedback


def add_internal_note(feedback: Feedback, actor, note: str) -> Feedback:
    if _actor_role(actor) == "user":
        raise PermissionDenied("Users cannot add internal notes.")
    if not note.strip():
        raise FeedbackActionError("Note cannot be empty.")
    stamp = timezone.now().strftime("%Y-%m-%d %H:%M")
    entry = f"[{stamp} — {actor.username}] {note.strip()}"
    feedback.internal_notes = f"{feedback.internal_notes}\n\n{entry}".strip()
    feedback.save(update_fields=["internal_notes", "updated_at"])
    # Deliberately no notification — internal-note edits must never
    # reach the reporter (see feedback.tasks.notify_reporter's own
    # "material change" filter, which excludes this event type too).
    _record_event(feedback, actor, FeedbackEventType.NOTE_ADDED, {})
    return feedback


def post_response(feedback: Feedback, actor, response_text: str) -> Feedback:
    if _actor_role(actor) == "user":
        raise PermissionDenied("Users cannot post an Admin response.")
    if not response_text.strip():
        raise FeedbackActionError("Response cannot be empty.")
    feedback.admin_response = response_text.strip()
    feedback.save(update_fields=["admin_response", "updated_at"])
    _record_event(feedback, actor, FeedbackEventType.RESPONSE_POSTED, {})

    from feedback.tasks import notify_reporter

    notify_reporter.delay(feedback.id, "response_posted")
    return feedback


def mark_duplicate(feedback: Feedback, actor, original: Feedback) -> Feedback:
    if _actor_role(actor) == "user":
        raise PermissionDenied("Users cannot mark feedback as a duplicate.")
    if original.id == feedback.id:
        raise FeedbackActionError("Feedback cannot be a duplicate of itself.")
    feedback.duplicate_of = original
    feedback.status = FeedbackStatus.DUPLICATE
    if feedback.resolved_at is None:
        feedback.resolved_at = timezone.now()
    feedback.save(update_fields=["duplicate_of", "status", "resolved_at", "updated_at"])
    _record_event(feedback, actor, FeedbackEventType.MARKED_DUPLICATE, {"original_reference": original.reference_number})

    from feedback.tasks import notify_reporter

    notify_reporter.delay(feedback.id, "status_changed")
    return feedback


def add_follow_up(feedback: Feedback, user, text: str) -> Feedback:
    """Reporter-added follow-up information — the spec's "Add follow-up
    information when allowed": allowed any time the item isn't closed,
    same window as withdrawal."""
    if feedback.reporter_id != user.id:
        raise PermissionDenied("You can only follow up on your own feedback.")
    if feedback.is_closed:
        raise FeedbackActionError("This item is closed — open a new report instead of following up.")
    if not text.strip():
        raise FeedbackActionError("Follow-up cannot be empty.")
    _record_event(feedback, user, FeedbackEventType.REPORTER_FOLLOW_UP, {"text": text.strip()[:2000]})
    Feedback.objects.filter(pk=feedback.pk).update(updated_at=timezone.now())
    return feedback


def withdraw(feedback: Feedback, user) -> Feedback:
    return set_status(feedback, user, FeedbackStatus.WITHDRAWN)


def dispute_resolution(feedback: Feedback, user) -> Feedback:
    """"Indicate that a resolved issue remains unresolved" — reopens a
    closed (Implemented/Resolved) item back to Under Review rather than
    silently doing nothing; Admin/Staff still make the actual call from
    there. Deliberately does not itself re-notify the reporter (they're
    the one acting) — it will surface in the staff/admin triage queue."""
    if feedback.reporter_id != user.id:
        raise PermissionDenied("You can only dispute the resolution of your own feedback.")
    if feedback.status not in (FeedbackStatus.RESOLVED, FeedbackStatus.IMPLEMENTED):
        raise FeedbackActionError("Only a Resolved or Implemented item can be marked still unresolved.")
    feedback.status = FeedbackStatus.UNDER_REVIEW
    feedback.resolved_at = None
    feedback.save(update_fields=["status", "resolved_at", "updated_at"])
    _record_event(feedback, user, FeedbackEventType.REPORTER_DISPUTED_RESOLUTION, {})
    return feedback


def admin_dashboard_summary() -> dict:
    from datetime import timedelta

    from django.db.models import Avg, Count, F
    from django.db.models.functions import TruncDate

    from feedback.models import FeedbackCategory

    qs = Feedback.objects.all()
    new_count = qs.filter(status=FeedbackStatus.NEW).count()
    awaiting_review_count = qs.filter(status__in=(FeedbackStatus.NEW, FeedbackStatus.UNDER_REVIEW)).count()
    urgent_count = qs.filter(reporter_priority="urgent").exclude(status__in=CLOSED_STATUSES).count()
    bugs_count = qs.filter(category=FeedbackCategory.BUG).exclude(status__in=CLOSED_STATUSES).count()
    feature_requests_count = qs.filter(category=FeedbackCategory.FEATURE_REQUEST).exclude(status__in=CLOSED_STATUSES).count()
    data_prediction_count = qs.filter(category__in=DIAGNOSTIC_CATEGORIES).exclude(status__in=CLOSED_STATUSES).count()
    in_progress_count = qs.filter(status=FeedbackStatus.IN_PROGRESS).count()
    recently_resolved = qs.filter(
        status__in=(FeedbackStatus.IMPLEMENTED, FeedbackStatus.RESOLVED),
        resolved_at__gte=timezone.now() - timedelta(days=14),
    ).count()

    reviewed = qs.filter(reviewed_at__isnull=False)
    avg_review_seconds = reviewed.annotate(gap=F("reviewed_at") - F("created_at")).aggregate(avg=Avg("gap"))["avg"]
    avg_review_hours = round(avg_review_seconds.total_seconds() / 3600, 1) if avg_review_seconds else None

    since = timezone.now() - timedelta(days=30)
    volume_by_day = list(
        qs.filter(created_at__gte=since)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(count=Count("id"))
        .order_by("day")
    )
    top_pages = list(
        qs.exclude(page_path="").values("page_path").annotate(count=Count("id")).order_by("-count")[:10]
    )

    return {
        "new_count": new_count,
        "awaiting_review_count": awaiting_review_count,
        "urgent_count": urgent_count,
        "bugs_count": bugs_count,
        "feature_requests_count": feature_requests_count,
        "data_prediction_count": data_prediction_count,
        "in_progress_count": in_progress_count,
        "recently_resolved_count": recently_resolved,
        "avg_review_hours": avg_review_hours,
        "volume_by_day": volume_by_day,
        "top_pages": top_pages,
    }
