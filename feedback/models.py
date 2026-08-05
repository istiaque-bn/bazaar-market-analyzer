"""User/Staff feedback & improvement-request system.

Deliberately reuses the project's existing patterns rather than
inventing new ones: a username-snapshot + SET_NULL FK so a record
outlives the account it describes (same shape as
market.models.AdminAuditLog), and a separate append-only FeedbackEvent
audit trail for status/priority/assignment changes (same idea as
AdminAuditLog, scoped to one Feedback row instead of the whole site).
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class FeedbackCategory(models.TextChoices):
    BUG = "bug", "Bug"
    FEATURE_REQUEST = "feature_request", "Feature Request"
    DATA_ISSUE = "data_issue", "Data Issue"
    PREDICTION_ISSUE = "prediction_issue", "Prediction Issue"
    PORTFOLIO_ISSUE = "portfolio_issue", "Portfolio Issue"
    UI_ISSUE = "ui_issue", "User Interface Issue"
    PERFORMANCE_ISSUE = "performance_issue", "Performance Issue"
    ACCOUNT_ISSUE = "account_issue", "Account Issue"
    OTHER = "other", "Other"


# Categories that may carry server-captured prediction/data diagnostic
# metadata (see feedback.services.capture_diagnostic_metadata) — every
# other category leaves those fields blank.
DIAGNOSTIC_CATEGORIES = (FeedbackCategory.DATA_ISSUE, FeedbackCategory.PREDICTION_ISSUE)


class FeedbackPriority(models.TextChoices):
    LOW = "low", "Low"
    NORMAL = "normal", "Normal"
    HIGH = "high", "High"
    URGENT = "urgent", "Urgent"


class FeedbackStatus(models.TextChoices):
    NEW = "new", "New"
    UNDER_REVIEW = "under_review", "Under Review"
    PLANNED = "planned", "Planned"
    IN_PROGRESS = "in_progress", "In Progress"
    IMPLEMENTED = "implemented", "Implemented"
    RESOLVED = "resolved", "Resolved"
    DUPLICATE = "duplicate", "Duplicate"
    CANNOT_REPRODUCE = "cannot_reproduce", "Cannot Reproduce"
    DECLINED = "declined", "Declined"
    WITHDRAWN = "withdrawn", "Withdrawn"


CLOSED_STATUSES = frozenset(
    {
        FeedbackStatus.IMPLEMENTED,
        FeedbackStatus.RESOLVED,
        FeedbackStatus.DUPLICATE,
        FeedbackStatus.CANNOT_REPRODUCE,
        FeedbackStatus.DECLINED,
        FeedbackStatus.WITHDRAWN,
    }
)

# Which non-final statuses a regular User may set (self-service only —
# see feedback.services.set_status for the actual enforcement) and which
# a Staff member may move an item through during triage. Statuses that
# declare a final resolution (Implemented/Resolved/Declined) are
# Admin-only — Admin's own allowed set is "anything" (validated as a
# member of FeedbackStatus, not restricted to this list).
USER_ALLOWED_STATUSES = frozenset({FeedbackStatus.WITHDRAWN})
STAFF_ALLOWED_STATUSES = frozenset(
    {
        FeedbackStatus.UNDER_REVIEW,
        FeedbackStatus.IN_PROGRESS,
        FeedbackStatus.DUPLICATE,
        FeedbackStatus.CANNOT_REPRODUCE,
    }
)


class Feedback(models.Model):
    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="feedback_submitted"
    )
    # Captured at creation time so the record stays legible/attributable
    # even if the account is later deleted — same rationale as
    # AdminAuditLog.username_snapshot.
    reporter_username_snapshot = models.CharField(max_length=150, blank=True)
    reference_number = models.CharField(max_length=16, unique=True, editable=False, db_index=True)

    category = models.CharField(max_length=32, choices=FeedbackCategory.choices, db_index=True)
    title = models.CharField(max_length=140)
    description = models.TextField(max_length=5000)
    reporter_priority = models.CharField(max_length=16, choices=FeedbackPriority.choices, default=FeedbackPriority.NORMAL)
    # blank = "Admin hasn't set a final priority yet" — deliberately not
    # defaulted to a FeedbackPriority value so the admin queue can tell
    # "not yet triaged" apart from "explicitly set to Low".
    admin_priority = models.CharField(max_length=16, choices=FeedbackPriority.choices, blank=True)
    status = models.CharField(max_length=20, choices=FeedbackStatus.choices, default=FeedbackStatus.NEW, db_index=True)

    page_path = models.CharField(max_length=255, blank=True)
    steps_to_reproduce = models.TextField(max_length=3000, blank=True)
    expected_behavior = models.TextField(max_length=2000, blank=True)
    actual_behavior = models.TextField(max_length=2000, blank=True)

    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="feedback_assigned",
        limit_choices_to={"is_staff": True},
    )
    duplicate_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="duplicates"
    )

    # Public — shown to the reporter. Distinct from internal_notes, which
    # never is (see feedback/views.py's queryset/serialization boundaries).
    admin_response = models.TextField(max_length=3000, blank=True)
    internal_notes = models.TextField(blank=True)

    contact_allowed = models.BooleanField(default=True)

    # --- Server-captured diagnostic metadata (Prediction/Data Issue only)
    # Populated exclusively from database lookups in
    # feedback.services.capture_diagnostic_metadata — never trusted from
    # a submitted (and therefore forgeable) hidden form field.
    meta_exchange = models.CharField(max_length=3, blank=True)
    meta_trading_code = models.CharField(max_length=32, blank=True)
    meta_analysis_date = models.DateField(null=True, blank=True)
    meta_model_version = models.CharField(max_length=32, blank=True)
    meta_quote_timestamp = models.DateTimeField(null=True, blank=True)
    meta_data_source = models.CharField(max_length=64, blank=True)
    meta_prediction_reference = models.CharField(max_length=64, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="feedback_status_created_idx"),
            models.Index(fields=["category", "-created_at"], name="feedback_category_created_idx"),
            models.Index(fields=["reporter", "-created_at"], name="feedback_reporter_created_idx"),
            models.Index(fields=["admin_priority", "-created_at"], name="feedback_adminprio_created_idx"),
        ]

    def __str__(self):
        return f"{self.reference_number or 'FB-pending'}: {self.title}"

    def save(self, *args, **kwargs):
        creating = self.pk is None
        super().save(*args, **kwargs)
        if creating and not self.reference_number:
            # Reference number is derived from the PK *after* the first
            # insert (so it's guaranteed unique without a separate
            # counter table/race condition), then persisted in a second,
            # narrow update — mirrors the common Django
            # id-derived-reference-code pattern.
            self.reference_number = f"FB-{self.pk:06d}"
            super().save(update_fields=["reference_number"])

    @property
    def is_closed(self) -> bool:
        return self.status in CLOSED_STATUSES


class FeedbackEventType(models.TextChoices):
    CREATED = "created", "Created"
    STATUS_CHANGED = "status_changed", "Status changed"
    PRIORITY_SET = "priority_set", "Priority set"
    ASSIGNED = "assigned", "Assigned"
    NOTE_ADDED = "note_added", "Internal note added"
    RESPONSE_POSTED = "response_posted", "Response posted"
    REPORTER_FOLLOW_UP = "reporter_follow_up", "Reporter follow-up"
    REPORTER_DISPUTED_RESOLUTION = "reporter_disputed_resolution", "Reporter marked still unresolved"
    MARKED_DUPLICATE = "marked_duplicate", "Marked duplicate"


# Event types a regular User (viewing only their own feedback) is
# allowed to see — excludes internal-note edits and raw assignment
# bookkeeping, matching "Keep internal notes hidden from regular Users."
REPORTER_VISIBLE_EVENT_TYPES = frozenset(
    {
        FeedbackEventType.CREATED,
        FeedbackEventType.STATUS_CHANGED,
        FeedbackEventType.RESPONSE_POSTED,
        FeedbackEventType.REPORTER_FOLLOW_UP,
        FeedbackEventType.REPORTER_DISPUTED_RESOLUTION,
        FeedbackEventType.MARKED_DUPLICATE,
    }
)


class FeedbackEvent(models.Model):
    feedback = models.ForeignKey(Feedback, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    actor_username_snapshot = models.CharField(max_length=150, blank=True)
    event_type = models.CharField(max_length=32, choices=FeedbackEventType.choices)
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.feedback.reference_number} {self.event_type} @ {self.created_at:%Y-%m-%d %H:%M}"
