from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.decorators import admin_required, staff_or_admin_required
from accounts.roles import is_admin, is_staff_member
from feedback.forms import (
    AdminPriorityForm,
    AdminResponseForm,
    AssignForm,
    DuplicateForm,
    FeedbackSubmitForm,
    FollowUpForm,
    InternalNoteForm,
    StatusChangeForm,
)
from feedback.models import CLOSED_STATUSES, Feedback, FeedbackCategory, FeedbackStatus
from feedback.services import (
    FeedbackActionError,
    add_follow_up,
    add_internal_note,
    assign,
    create_feedback,
    dispute_resolution,
    mark_duplicate,
    post_response,
    set_admin_priority,
    set_status,
    withdraw,
)
from market.services.rate_limit import is_rate_limited

# Category triage staff cannot see/act on — see feedback/views.py's
# module docstring rationale in README's "Feedback permissions" section:
# account-issue reports can reference another user's account details in
# free text, so they're routed to Admin only.
STAFF_RESTRICTED_CATEGORIES = (FeedbackCategory.ACCOUNT_ISSUE,)


def _staff_queryset():
    return Feedback.objects.exclude(category__in=STAFF_RESTRICTED_CATEGORIES)


def _visible_events(feedback, viewer_is_staff: bool):
    qs = feedback.events.select_related("actor")
    if viewer_is_staff:
        return qs
    from feedback.models import REPORTER_VISIBLE_EVENT_TYPES

    return qs.filter(event_type__in=REPORTER_VISIBLE_EVENT_TYPES)


# ---------------------------------------------------------------------------
# Reporter (User + Staff, for their own submissions)
# ---------------------------------------------------------------------------


@login_required
def submit(request):
    if request.method == "POST":
        if is_rate_limited(f"feedback_submit:{request.user.id}", limit=5, period_seconds=3600):
            messages.error(request, "You've submitted several reports recently — please wait before submitting another.")
            return redirect("feedback_my_list")
        form = FeedbackSubmitForm(request.POST)
        if form.is_valid():
            c = form.cleaned_data
            fb = create_feedback(
                request.user,
                category=c["category"],
                title=c["title"],
                description=c["description"],
                reporter_priority=c["reporter_priority"],
                page_path=c["page_path"],
                steps_to_reproduce=c["steps_to_reproduce"],
                expected_behavior=c["expected_behavior"],
                actual_behavior=c["actual_behavior"],
                contact_allowed=c["contact_allowed"],
                meta_exchange=c["meta_exchange"],
                meta_trading_code=c["meta_trading_code"],
            )
            messages.success(request, f"Thanks — your report {fb.reference_number} has been received.")
            return redirect("feedback_detail", pk=fb.pk)
    else:
        initial = {"page_path": request.GET.get("from", "")[:255]}
        form = FeedbackSubmitForm(initial=initial)
    return render(request, "feedback/submit.html", {"form": form})


@login_required
def my_list(request):
    qs = Feedback.objects.filter(reporter=request.user)
    status = request.GET.get("status", "")
    if status:
        qs = qs.filter(status=status)
    page = Paginator(qs, 20).get_page(request.GET.get("page"))
    return render(request, "feedback/my_list.html", {"page_obj": page, "status": status})


@login_required
def detail(request, pk):
    """Shared detail page: a reporter sees only their own submission; a
    Staff/Admin viewer sees any (Staff excluded from
    STAFF_RESTRICTED_CATEGORIES) — enforced via the lookup queryset
    itself, so a wrong id 404s exactly like a nonexistent one rather
    than leaking whether it belongs to someone else."""
    is_triage_viewer = is_admin(request.user) or is_staff_member(request.user)
    if is_triage_viewer:
        qs = Feedback.objects.all() if is_admin(request.user) else _staff_queryset()
        fb = get_object_or_404(qs, pk=pk)
    else:
        fb = get_object_or_404(Feedback, pk=pk, reporter=request.user)

    events = _visible_events(fb, is_triage_viewer)
    context = {
        "fb": fb,
        "events": events,
        "is_owner": fb.reporter_id == request.user.id,
        "is_triage_viewer": is_triage_viewer,
        "can_manage_priority": is_admin(request.user),
        "follow_up_form": FollowUpForm(),
        "status_form": StatusChangeForm(),
        "note_form": InternalNoteForm(),
        "response_form": AdminResponseForm(initial={"response": fb.admin_response}),
        "priority_form": AdminPriorityForm(initial={"admin_priority": fb.admin_priority}),
        "assign_form": AssignForm(),
        "duplicate_form": DuplicateForm(),
    }
    if is_triage_viewer:
        return render(request, "feedback/triage_detail.html", context)
    return render(request, "feedback/detail.html", context)


@login_required
@require_POST
def follow_up(request, pk):
    fb = get_object_or_404(Feedback, pk=pk, reporter=request.user)
    if is_rate_limited(f"feedback_comment:{request.user.id}", limit=20, period_seconds=3600):
        messages.error(request, "Too many updates in a short time — please slow down.")
        return redirect("feedback_detail", pk=fb.pk)
    form = FollowUpForm(request.POST)
    if form.is_valid():
        try:
            add_follow_up(fb, request.user, form.cleaned_data["text"])
            messages.success(request, "Follow-up added.")
        except (PermissionDenied, FeedbackActionError) as exc:
            messages.error(request, str(exc))
    else:
        messages.error(request, "Follow-up couldn't be saved — please check the form.")
    return redirect("feedback_detail", pk=fb.pk)


@login_required
@require_POST
def withdraw_view(request, pk):
    fb = get_object_or_404(Feedback, pk=pk, reporter=request.user)
    try:
        withdraw(fb, request.user)
        messages.success(request, "Your report has been withdrawn.")
    except (PermissionDenied, FeedbackActionError) as exc:
        messages.error(request, str(exc))
    return redirect("feedback_detail", pk=fb.pk)


@login_required
@require_POST
def dispute_view(request, pk):
    fb = get_object_or_404(Feedback, pk=pk, reporter=request.user)
    try:
        dispute_resolution(fb, request.user)
        messages.success(request, "Marked as still unresolved — it's back in the review queue.")
    except (PermissionDenied, FeedbackActionError) as exc:
        messages.error(request, str(exc))
    return redirect("feedback_detail", pk=fb.pk)


# ---------------------------------------------------------------------------
# Staff + Admin triage
# ---------------------------------------------------------------------------


@staff_or_admin_required
def triage_list(request):
    qs = Feedback.objects.all() if is_admin(request.user) else _staff_queryset()

    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(reference_number__icontains=q) | Q(title__icontains=q) | Q(description__icontains=q)
            | Q(reporter_username_snapshot__icontains=q)
        )
    category = request.GET.get("category", "")
    if category:
        qs = qs.filter(category=category)
    status = request.GET.get("status", "")
    if status:
        qs = qs.filter(status=status)
    reporter_priority = request.GET.get("reporter_priority", "")
    if reporter_priority:
        qs = qs.filter(reporter_priority=reporter_priority)
    admin_priority = request.GET.get("admin_priority", "")
    if admin_priority:
        qs = qs.filter(admin_priority=admin_priority)
    assigned = request.GET.get("assigned", "")
    if assigned == "me":
        qs = qs.filter(assigned_to=request.user)
    elif assigned == "unassigned":
        qs = qs.filter(assigned_to__isnull=True)
    date_from = request.GET.get("date_from", "")
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    date_to = request.GET.get("date_to", "")
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)
    page_filter = request.GET.get("page_path", "")
    if page_filter:
        qs = qs.filter(page_path__icontains=page_filter)

    page = Paginator(qs.select_related("reporter", "assigned_to"), 25).get_page(request.GET.get("page"))
    return render(
        request,
        "feedback/triage_list.html",
        {
            "page_obj": page,
            "q": q,
            "category": category,
            "status": status,
            "reporter_priority": reporter_priority,
            "admin_priority": admin_priority,
            "assigned": assigned,
            "date_from": date_from,
            "date_to": date_to,
            "page_path": page_filter,
            "categories": FeedbackCategory.choices,
            "statuses": FeedbackStatus.choices,
        },
    )


def _triage_get_object(request, pk):
    qs = Feedback.objects.all() if is_admin(request.user) else _staff_queryset()
    return get_object_or_404(qs, pk=pk)


@staff_or_admin_required
@require_POST
def change_status(request, pk):
    fb = _triage_get_object(request, pk)
    form = StatusChangeForm(request.POST)
    if form.is_valid():
        try:
            set_status(fb, request.user, form.cleaned_data["status"], form.cleaned_data.get("note", ""))
            messages.success(request, "Status updated.")
        except (PermissionDenied, FeedbackActionError) as exc:
            messages.error(request, str(exc))
    else:
        messages.error(request, "Invalid status change.")
    return redirect("feedback_detail", pk=fb.pk)


@staff_or_admin_required
@require_POST
def add_note(request, pk):
    fb = _triage_get_object(request, pk)
    form = InternalNoteForm(request.POST)
    if form.is_valid():
        try:
            add_internal_note(fb, request.user, form.cleaned_data["note"])
            messages.success(request, "Internal note added.")
        except (PermissionDenied, FeedbackActionError) as exc:
            messages.error(request, str(exc))
    else:
        messages.error(request, "Note couldn't be saved.")
    return redirect("feedback_detail", pk=fb.pk)


@staff_or_admin_required
@require_POST
def assign_view(request, pk):
    fb = _triage_get_object(request, pk)
    form = AssignForm(request.POST)
    if form.is_valid():
        from django.contrib.auth.models import User

        assignee = get_object_or_404(User, pk=form.cleaned_data["assignee_id"])
        try:
            assign(fb, request.user, assignee)
            messages.success(request, f"Assigned to {assignee.username}.")
        except (PermissionDenied, FeedbackActionError) as exc:
            messages.error(request, str(exc))
    else:
        messages.error(request, "Invalid assignment.")
    return redirect("feedback_detail", pk=fb.pk)


@staff_or_admin_required
@require_POST
def assign_to_me(request, pk):
    fb = _triage_get_object(request, pk)
    try:
        assign(fb, request.user, request.user)
        messages.success(request, "Assigned to you.")
    except (PermissionDenied, FeedbackActionError) as exc:
        messages.error(request, str(exc))
    return redirect("feedback_detail", pk=fb.pk)


@staff_or_admin_required
@require_POST
def post_response_view(request, pk):
    fb = _triage_get_object(request, pk)
    form = AdminResponseForm(request.POST)
    if form.is_valid():
        try:
            post_response(fb, request.user, form.cleaned_data["response"])
            messages.success(request, "Response posted — the reporter has been notified.")
        except (PermissionDenied, FeedbackActionError) as exc:
            messages.error(request, str(exc))
    else:
        messages.error(request, "Response couldn't be saved.")
    return redirect("feedback_detail", pk=fb.pk)


# ---------------------------------------------------------------------------
# Admin only
# ---------------------------------------------------------------------------


@admin_required
@require_POST
def set_priority(request, pk):
    fb = get_object_or_404(Feedback, pk=pk)
    form = AdminPriorityForm(request.POST)
    if form.is_valid():
        try:
            set_admin_priority(fb, request.user, form.cleaned_data["admin_priority"])
            messages.success(request, "Priority set.")
        except (PermissionDenied, FeedbackActionError) as exc:
            messages.error(request, str(exc))
    else:
        messages.error(request, "Invalid priority.")
    return redirect("feedback_detail", pk=fb.pk)


@admin_required
@require_POST
def mark_duplicate_view(request, pk):
    fb = get_object_or_404(Feedback, pk=pk)
    form = DuplicateForm(request.POST)
    if form.is_valid():
        original = Feedback.objects.filter(reference_number=form.cleaned_data["original_reference"]).first()
        if original is None:
            messages.error(request, "No feedback found with that reference number.")
        else:
            try:
                mark_duplicate(fb, request.user, original)
                messages.success(request, f"Marked as a duplicate of {original.reference_number}.")
            except (PermissionDenied, FeedbackActionError) as exc:
                messages.error(request, str(exc))
    else:
        messages.error(request, "Invalid reference number.")
    return redirect("feedback_detail", pk=fb.pk)


@admin_required
def admin_dashboard(request):
    from feedback.services import admin_dashboard_summary

    return render(request, "feedback/admin_dashboard.html", {"summary": admin_dashboard_summary()})


@admin_required
def export(request):
    """A safe summary CSV for upgrade planning — reference number,
    category, both priorities, status, title, created/resolved dates
    only. Deliberately excludes description/steps/internal_notes/
    reporter identity/contact info/attachments — see README's "Feedback"
    section for the documented export scope."""
    import csv

    from django.http import HttpResponse

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="feedback_summary.csv"'
    writer = csv.writer(response)
    writer.writerow(["reference_number", "category", "reporter_priority", "admin_priority", "status", "title", "created_at", "resolved_at"])
    for fb in Feedback.objects.order_by("-created_at").iterator():
        writer.writerow(
            [
                fb.reference_number,
                fb.get_category_display(),
                fb.get_reporter_priority_display(),
                fb.get_admin_priority_display() if fb.admin_priority else "",
                fb.get_status_display(),
                fb.title,
                fb.created_at.isoformat(),
                fb.resolved_at.isoformat() if fb.resolved_at else "",
            ]
        )
    return response
