"""Feedback system tests: authentication/ownership, role-gated triage
transitions, mass-assignment defense, notifications, rate limiting, and
"feedback never touches market/ML data"."""
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from feedback.models import (
    Feedback,
    FeedbackEvent,
    FeedbackEventType,
    FeedbackStatus,
)
from feedback.services import (
    FeedbackActionError,
    add_internal_note,
    create_feedback,
    dispute_resolution,
    post_response,
    set_admin_priority,
    set_status,
    withdraw,
)
from market.models import AnalysisResult, Exchange, MLModelVersion, SignalAction, Stock
from notifications.models import Alert

PASSWORD = "Correct-Horse-Battery-Staple-42"


def make_admin(username="fb_admin") -> User:
    return User.objects.create_user(username=username, password=PASSWORD, is_staff=True, is_superuser=True)


def make_staff(username="fb_staff") -> User:
    return User.objects.create_user(username=username, password=PASSWORD, is_staff=True)


def make_user(username="fb_user") -> User:
    return User.objects.create_user(username=username, password=PASSWORD)


class AnonymousAccessTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.fb = create_feedback(
            self.user, category="bug", title="Broken thing", description="It is broken in a bad way.",
            reporter_priority="normal",
        )

    def test_cannot_view_submit_form(self):
        response = self.client.get(reverse("feedback_submit"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_cannot_submit(self):
        before = Feedback.objects.count()
        response = self.client.post(
            reverse("feedback_submit"), {"category": "bug", "title": "Hacked", "description": "x" * 20, "reporter_priority": "low"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Feedback.objects.count(), before)

    def test_cannot_view_others_feedback(self):
        response = self.client.get(reverse("feedback_detail", args=[self.fb.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_cannot_view_triage_list(self):
        response = self.client.get(reverse("feedback_triage_list"))
        self.assertEqual(response.status_code, 302)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class SubmissionTests(TestCase):
    def setUp(self):
        # The rate limiter uses Django's process-global cache, not the
        # per-test DB transaction rollback — without clearing it, a
        # user id recycled across tests (SQLite reuses rowids after a
        # rolled-back insert) can inherit another test's rate-limit
        # bucket and spuriously 302 instead of behaving normally.
        cache.clear()
        self.user = make_user("submitter")
        self.client.login(username="submitter", password=PASSWORD)

    def test_valid_submission_creates_feedback_with_reference_number(self):
        response = self.client.post(
            reverse("feedback_submit"),
            {
                "category": "bug", "title": "Chart is blank", "description": "The candlestick chart never renders.",
                "reporter_priority": "high", "contact_allowed": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        fb = Feedback.objects.get()
        self.assertRegex(fb.reference_number, r"^FB-\d{6}$")
        self.assertEqual(fb.reporter, self.user)
        self.assertEqual(fb.status, FeedbackStatus.NEW)

    def test_too_short_title_rejected(self):
        response = self.client.post(
            reverse("feedback_submit"),
            {"category": "bug", "title": "Hi", "description": "x" * 20, "reporter_priority": "low"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Feedback.objects.exists())

    def test_oversized_description_rejected(self):
        response = self.client.post(
            reverse("feedback_submit"),
            {"category": "bug", "title": "Valid title", "description": "x" * 6000, "reporter_priority": "low"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Feedback.objects.exists())

    def test_invalid_category_rejected(self):
        response = self.client.post(
            reverse("feedback_submit"),
            {"category": "not-a-real-category", "title": "Valid title", "description": "x" * 20, "reporter_priority": "low"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Feedback.objects.exists())

    def test_mass_assignment_of_admin_fields_is_ignored(self):
        """The submit form has no status/admin_priority/assigned_to/
        internal_notes fields at all — posting them must have zero
        effect, not merely be overridden."""
        response = self.client.post(
            reverse("feedback_submit"),
            {
                "category": "bug", "title": "Valid title here", "description": "x" * 20, "reporter_priority": "low",
                "status": "implemented", "admin_priority": "urgent", "assigned_to": "1",
                "internal_notes": "should never appear",
            },
        )
        self.assertEqual(response.status_code, 302)
        fb = Feedback.objects.get()
        self.assertEqual(fb.status, FeedbackStatus.NEW)
        self.assertEqual(fb.admin_priority, "")
        self.assertIsNone(fb.assigned_to)
        self.assertEqual(fb.internal_notes, "")

    def test_reporter_receives_received_notification(self):
        self.client.post(
            reverse("feedback_submit"),
            {"category": "bug", "title": "Valid title here", "description": "x" * 20, "reporter_priority": "low"},
        )
        self.assertTrue(Alert.objects.filter(user=self.user).exists())

    def test_rate_limit_blocks_after_threshold(self):
        cache.clear()
        for i in range(5):
            self.client.post(
                reverse("feedback_submit"),
                {"category": "bug", "title": f"Report number {i}", "description": "x" * 20, "reporter_priority": "low"},
            )
        before = Feedback.objects.count()
        self.client.post(
            reverse("feedback_submit"),
            {"category": "bug", "title": "One too many", "description": "x" * 20, "reporter_priority": "low"},
        )
        self.assertEqual(Feedback.objects.count(), before)

    def test_description_is_escaped_not_rendered_as_html(self):
        fb = create_feedback(
            self.user, category="bug", title="XSS attempt",
            description="<script>alert(1)</script> and more filler text",
            reporter_priority="low",
        )
        response = self.client.get(reverse("feedback_detail", args=[fb.pk]))
        content = response.content.decode()
        self.assertNotIn("<script>alert(1)</script>", content)
        self.assertIn("&lt;script&gt;", content)


class OwnershipTests(TestCase):
    def setUp(self):
        self.alice = make_user("owner_alice")
        self.bob = make_user("owner_bob")
        self.fb = create_feedback(
            self.alice, category="bug", title="Alice's bug", description="Something broke for alice specifically.",
            reporter_priority="normal",
        )

    def test_user_sees_only_own_submissions_in_my_list(self):
        create_feedback(self.bob, category="bug", title="Bob's bug", description="Something broke for bob too here.", reporter_priority="normal")
        self.client.login(username="owner_alice", password=PASSWORD)
        response = self.client.get(reverse("feedback_my_list"))
        titles = {fb.title for fb in response.context["page_obj"].object_list}
        self.assertEqual(titles, {"Alice's bug"})

    def test_other_user_cannot_view_detail(self):
        self.client.login(username="owner_bob", password=PASSWORD)
        response = self.client.get(reverse("feedback_detail", args=[self.fb.pk]))
        self.assertEqual(response.status_code, 404)

    def test_other_user_cannot_follow_up(self):
        self.client.login(username="owner_bob", password=PASSWORD)
        response = self.client.post(reverse("feedback_follow_up", args=[self.fb.pk]), {"text": "not mine to comment on"})
        self.assertEqual(response.status_code, 404)

    def test_other_user_cannot_withdraw(self):
        self.client.login(username="owner_bob", password=PASSWORD)
        response = self.client.post(reverse("feedback_withdraw", args=[self.fb.pk]))
        self.assertEqual(response.status_code, 404)
        self.fb.refresh_from_db()
        self.assertNotEqual(self.fb.status, FeedbackStatus.WITHDRAWN)

    def test_owner_can_withdraw_unresolved(self):
        self.client.login(username="owner_alice", password=PASSWORD)
        response = self.client.post(reverse("feedback_withdraw", args=[self.fb.pk]))
        self.assertEqual(response.status_code, 302)
        self.fb.refresh_from_db()
        self.assertEqual(self.fb.status, FeedbackStatus.WITHDRAWN)

    def test_owner_can_follow_up(self):
        self.client.login(username="owner_alice", password=PASSWORD)
        response = self.client.post(reverse("feedback_follow_up", args=[self.fb.pk]), {"text": "Extra detail from alice."})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            FeedbackEvent.objects.filter(feedback=self.fb, event_type=FeedbackEventType.REPORTER_FOLLOW_UP).exists()
        )

    def test_user_cannot_set_admin_priority_via_service(self):
        with self.assertRaises(Exception):
            set_admin_priority(self.fb, self.alice, "urgent")

    def test_user_cannot_set_disallowed_status_via_service(self):
        with self.assertRaises(Exception):
            set_status(self.fb, self.alice, FeedbackStatus.RESOLVED)

    def test_internal_notes_never_in_reporter_context(self):
        staff = make_staff("owner_staff")
        add_internal_note(self.fb, staff, "secret internal detail")
        self.client.login(username="owner_alice", password=PASSWORD)
        response = self.client.get(reverse("feedback_detail", args=[self.fb.pk]))
        self.assertNotIn("secret internal detail", response.content.decode())
        # Also not present anywhere in the rendered context's event list
        for e in response.context["events"]:
            self.assertNotEqual(e.event_type, FeedbackEventType.NOTE_ADDED)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class StaffTriageTests(TestCase):
    def setUp(self):
        self.staff = make_staff("triage_staff")
        self.reporter = make_user("triage_reporter")
        self.fb = create_feedback(
            self.reporter, category="bug", title="Needs triage", description="Please take a look at this issue.",
            reporter_priority="normal",
        )
        self.client.login(username="triage_staff", password=PASSWORD)

    def test_staff_can_view_triage_list(self):
        response = self.client.get(reverse("feedback_triage_list"))
        self.assertEqual(response.status_code, 200)

    def test_staff_can_move_to_under_review(self):
        response = self.client.post(reverse("feedback_change_status", args=[self.fb.pk]), {"status": "under_review"})
        self.assertEqual(response.status_code, 302)
        self.fb.refresh_from_db()
        self.assertEqual(self.fb.status, FeedbackStatus.UNDER_REVIEW)

    def test_staff_cannot_mark_implemented(self):
        response = self.client.post(reverse("feedback_change_status", args=[self.fb.pk]), {"status": "implemented"})
        self.assertEqual(response.status_code, 302)
        self.fb.refresh_from_db()
        self.assertNotEqual(self.fb.status, FeedbackStatus.IMPLEMENTED)

    def test_staff_cannot_mark_resolved(self):
        response = self.client.post(reverse("feedback_change_status", args=[self.fb.pk]), {"status": "resolved"})
        self.fb.refresh_from_db()
        self.assertNotEqual(self.fb.status, FeedbackStatus.RESOLVED)

    def test_staff_cannot_set_admin_priority(self):
        response = self.client.post(reverse("feedback_set_priority", args=[self.fb.pk]), {"admin_priority": "urgent"})
        self.assertEqual(response.status_code, 403)
        self.fb.refresh_from_db()
        self.assertEqual(self.fb.admin_priority, "")

    def test_staff_can_assign_to_self(self):
        response = self.client.post(reverse("feedback_assign_to_me", args=[self.fb.pk]))
        self.assertEqual(response.status_code, 302)
        self.fb.refresh_from_db()
        self.assertEqual(self.fb.assigned_to, self.staff)

    def test_staff_cannot_assign_to_someone_else(self):
        other_staff = make_staff("other_triage_staff")
        response = self.client.post(reverse("feedback_assign", args=[self.fb.pk]), {"assignee_id": other_staff.id})
        self.fb.refresh_from_db()
        self.assertNotEqual(self.fb.assigned_to, other_staff)

    def test_staff_can_add_internal_note(self):
        response = self.client.post(reverse("feedback_add_note", args=[self.fb.pk]), {"note": "Investigating."})
        self.assertEqual(response.status_code, 302)
        self.fb.refresh_from_db()
        self.assertIn("Investigating.", self.fb.internal_notes)

    def test_staff_cannot_mark_duplicate(self):
        other = create_feedback(self.reporter, category="bug", title="Original report", description="This is the original one.", reporter_priority="normal")
        response = self.client.post(reverse("feedback_mark_duplicate", args=[self.fb.pk]), {"original_reference": other.reference_number})
        self.assertEqual(response.status_code, 403)

    def test_staff_cannot_access_account_issue_category(self):
        acct_fb = create_feedback(
            self.reporter, category="account_issue", title="Account problem", description="I cannot access my account settings.",
            reporter_priority="high",
        )
        response = self.client.get(reverse("feedback_detail", args=[acct_fb.pk]))
        self.assertEqual(response.status_code, 404)
        response = self.client.get(reverse("feedback_triage_list"))
        self.assertNotIn(acct_fb, response.context["page_obj"].object_list)

    def test_note_added_does_not_notify_reporter(self):
        Alert.objects.filter(user=self.reporter).delete()
        self.client.post(reverse("feedback_add_note", args=[self.fb.pk]), {"note": "quiet internal note"})
        self.assertFalse(Alert.objects.filter(user=self.reporter).exists())

    def test_status_change_notifies_reporter(self):
        Alert.objects.filter(user=self.reporter).delete()
        self.client.post(reverse("feedback_change_status", args=[self.fb.pk]), {"status": "under_review"})
        self.assertTrue(Alert.objects.filter(user=self.reporter).exists())


class AdminTriageTests(TestCase):
    def setUp(self):
        self.admin = make_admin("triage_admin")
        self.reporter = make_user("admin_triage_reporter")
        self.fb = create_feedback(
            self.reporter, category="feature_request", title="Add dark mode", description="Please add a dark color theme option.",
            reporter_priority="low",
        )
        self.client.login(username="triage_admin", password=PASSWORD)

    def test_admin_can_set_priority(self):
        response = self.client.post(reverse("feedback_set_priority", args=[self.fb.pk]), {"admin_priority": "high"})
        self.assertEqual(response.status_code, 302)
        self.fb.refresh_from_db()
        self.assertEqual(self.fb.admin_priority, "high")

    def test_admin_can_assign_to_staff(self):
        staff = make_staff("assignable_staff")
        response = self.client.post(reverse("feedback_assign", args=[self.fb.pk]), {"assignee_id": staff.id})
        self.assertEqual(response.status_code, 302)
        self.fb.refresh_from_db()
        self.assertEqual(self.fb.assigned_to, staff)

    def test_admin_can_mark_implemented(self):
        response = self.client.post(reverse("feedback_change_status", args=[self.fb.pk]), {"status": "implemented"})
        self.assertEqual(response.status_code, 302)
        self.fb.refresh_from_db()
        self.assertEqual(self.fb.status, FeedbackStatus.IMPLEMENTED)
        self.assertIsNotNone(self.fb.resolved_at)

    def test_status_change_creates_audit_event(self):
        self.client.post(reverse("feedback_change_status", args=[self.fb.pk]), {"status": "planned"})
        self.assertTrue(
            FeedbackEvent.objects.filter(feedback=self.fb, event_type=FeedbackEventType.STATUS_CHANGED).exists()
        )

    def test_admin_can_post_public_response(self):
        response = self.client.post(reverse("feedback_post_response", args=[self.fb.pk]), {"response": "Thanks, we're looking into this."})
        self.assertEqual(response.status_code, 302)
        self.fb.refresh_from_db()
        self.assertIn("Thanks", self.fb.admin_response)

    def test_admin_can_mark_duplicate(self):
        original = create_feedback(self.reporter, category="feature_request", title="Dark mode wanted", description="Same request as before here.", reporter_priority="low")
        response = self.client.post(reverse("feedback_mark_duplicate", args=[self.fb.pk]), {"original_reference": original.reference_number})
        self.assertEqual(response.status_code, 302)
        self.fb.refresh_from_db()
        self.assertEqual(self.fb.status, FeedbackStatus.DUPLICATE)
        self.assertEqual(self.fb.duplicate_of, original)

    def test_admin_can_access_account_issue_category(self):
        acct_fb = create_feedback(self.reporter, category="account_issue", title="Account help needed", description="I need help with my account access.", reporter_priority="normal")
        response = self.client.get(reverse("feedback_detail", args=[acct_fb.pk]))
        self.assertEqual(response.status_code, 200)

    def test_export_is_safe_csv(self):
        response = self.client.get(reverse("feedback_export"))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        header = body.splitlines()[0]
        self.assertIn(self.fb.reference_number, body)
        self.assertNotIn(PASSWORD, body)
        for forbidden in ("internal_notes", "description", "steps_to_reproduce", "reporter_username", "contact_allowed"):
            self.assertNotIn(forbidden, header)

    def test_regular_user_cannot_access_admin_dashboard(self):
        user = make_user("blocked_dashboard_user")
        self.client.logout()
        self.client.login(username="blocked_dashboard_user", password=PASSWORD)
        response = self.client.get(reverse("feedback_admin_dashboard"))
        self.assertEqual(response.status_code, 403)

    def test_staff_cannot_access_admin_dashboard(self):
        staff = make_staff("blocked_dashboard_staff")
        self.client.logout()
        self.client.login(username="blocked_dashboard_staff", password=PASSWORD)
        response = self.client.get(reverse("feedback_admin_dashboard"))
        self.assertEqual(response.status_code, 403)


class DisputeResolutionTests(TestCase):
    def setUp(self):
        self.admin = make_admin("dispute_admin")
        self.reporter = make_user("dispute_reporter")
        self.fb = create_feedback(self.reporter, category="bug", title="Some bug", description="A bug that got resolved apparently.", reporter_priority="normal")
        set_status(self.fb, self.admin, FeedbackStatus.RESOLVED)

    def test_reporter_can_dispute_resolution(self):
        self.client.login(username="dispute_reporter", password=PASSWORD)
        response = self.client.post(reverse("feedback_dispute", args=[self.fb.pk]))
        self.assertEqual(response.status_code, 302)
        self.fb.refresh_from_db()
        self.assertEqual(self.fb.status, FeedbackStatus.UNDER_REVIEW)

    def test_cannot_dispute_a_still_open_item(self):
        open_fb = create_feedback(self.reporter, category="bug", title="Open bug here", description="Still being looked at right now.", reporter_priority="normal")
        with self.assertRaises(FeedbackActionError):
            dispute_resolution(open_fb, self.reporter)


class PredictionDataMetadataTests(TestCase):
    """Prediction/Data Issue feedback captures real server-side metadata,
    never a client-forged value, and never touches market data itself."""

    def setUp(self):
        self.user = make_user("meta_user")
        self.stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="METAX", company_name="Meta Test Co", last_price=25.5)
        AnalysisResult.objects.create(stock=self.stock, as_of="2026-01-05", action=SignalAction.BUY, score=40, confidence=0.6)

    def test_diagnostic_metadata_captured_for_prediction_issue(self):
        fb = create_feedback(
            self.user, category="prediction_issue", title="Prediction looks wrong", description="The predicted direction seems off to me.",
            reporter_priority="normal", meta_exchange="dse", meta_trading_code="metax",
        )
        self.assertEqual(fb.meta_exchange, Exchange.DSE)
        self.assertEqual(fb.meta_trading_code, "METAX")
        self.assertIsNotNone(fb.meta_analysis_date)

    def test_no_metadata_captured_for_non_diagnostic_category(self):
        fb = create_feedback(
            self.user, category="bug", title="Unrelated bug report", description="Something else entirely broke today.",
            reporter_priority="normal", meta_exchange="dse", meta_trading_code="metax",
        )
        self.assertEqual(fb.meta_exchange, "")
        self.assertEqual(fb.meta_trading_code, "")

    def test_feedback_submission_does_not_alter_stock_or_analysis(self):
        before_price = self.stock.last_price
        before_count = AnalysisResult.objects.count()
        create_feedback(
            self.user, category="data_issue", title="Price looks stale", description="This price does not look current to me.",
            reporter_priority="normal", meta_exchange="dse", meta_trading_code="metax",
        )
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.last_price, before_price)
        self.assertEqual(AnalysisResult.objects.count(), before_count)

    def test_feedback_never_touches_ml_model_versions(self):
        before = MLModelVersion.objects.count()
        create_feedback(
            self.user, category="prediction_issue", title="Model seems off", description="The model output looks incorrect today.",
            reporter_priority="normal", meta_exchange="dse", meta_trading_code="metax",
        )
        self.assertEqual(MLModelVersion.objects.count(), before)


class ReferenceNumberTests(TestCase):
    def test_reference_number_is_stable_sequential_format(self):
        user = make_user("ref_user")
        fb1 = create_feedback(user, category="bug", title="First report here", description="This is the first report body text.", reporter_priority="low")
        fb2 = create_feedback(user, category="bug", title="Second report here", description="This is the second report body text.", reporter_priority="low")
        self.assertNotEqual(fb1.reference_number, fb2.reference_number)
        self.assertTrue(fb1.reference_number.startswith("FB-"))


class ReporterSnapshotSurvivesDeletionTests(TestCase):
    def test_feedback_survives_reporter_deletion(self):
        user = make_user("deletable_reporter")
        fb = create_feedback(user, category="bug", title="Report before deletion", description="This account will be deleted soon after.", reporter_priority="low")
        username = user.username
        user.delete()
        fb.refresh_from_db()
        self.assertIsNone(fb.reporter)
        self.assertEqual(fb.reporter_username_snapshot, username)
