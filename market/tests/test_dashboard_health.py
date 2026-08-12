"""The staff-only dashboard health-warning banner (market.views._dashboard_health_issue)."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from market.models import Exchange, MarketSnapshot, TaskRun, TaskStatus
from market.services.ops_alerts import STALE_DATA_DAYS
from market.views import _dashboard_health_issue


class DashboardHealthIssueHelperTests(TestCase):
    def test_no_as_of_and_no_task_runs_raises_nothing(self):
        self.assertIsNone(_dashboard_health_issue(None))

    def test_fresh_as_of_with_no_sync_errors_raises_nothing(self):
        TaskRun.objects.create(
            task_name="market.tasks.sync_live_market",
            status=TaskStatus.SUCCESS,
            detail={"ok": True, "dse": {"ok": True}, "cse": {"ok": True}},
        )
        self.assertIsNone(_dashboard_health_issue(timezone.localdate()))

    def test_stale_as_of_raises_staleness_message(self):
        old = timezone.localdate() - timedelta(days=STALE_DATA_DAYS + 2)
        issue = _dashboard_health_issue(old)
        self.assertIsNotNone(issue)
        self.assertIn("days old", issue)

    def test_fresh_as_of_but_silently_failing_sync_raises_sync_message(self):
        TaskRun.objects.create(
            task_name="market.tasks.sync_live_market",
            status=TaskStatus.SUCCESS,
            detail={"ok": False, "error": "cannot import name 'MarketHoliday'"},
        )
        issue = _dashboard_health_issue(timezone.localdate())
        self.assertIsNotNone(issue)
        self.assertIn("silently failing", issue)

    def test_failed_task_run_status_is_ignored_here(self):
        """A raised-exception run (status=FAILURE) is a different, already
        very visible failure mode — this helper only needs to catch the
        silent ok=False-but-status=success case."""
        TaskRun.objects.create(task_name="market.tasks.sync_live_market", status=TaskStatus.FAILURE, error="boom")
        self.assertIsNone(_dashboard_health_issue(timezone.localdate()))


class DashboardHealthBannerRenderingTests(TestCase):
    def setUp(self):
        self.url = reverse("dashboard")

    def test_anonymous_user_is_redirected_to_login(self):
        TaskRun.objects.create(
            task_name="market.tasks.sync_live_market",
            status=TaskStatus.SUCCESS,
            detail={"ok": False, "error": "boom"},
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_staff_user_sees_the_banner_when_sync_is_silently_failing(self):
        staff = User.objects.create_user(username="staffer", password="Correct-Horse-Battery-Staple-42")
        staff.is_staff = True
        staff.save(update_fields=["is_staff"])
        self.client.login(username="staffer", password="Correct-Horse-Battery-Staple-42")
        TaskRun.objects.create(
            task_name="market.tasks.sync_live_market",
            status=TaskStatus.SUCCESS,
            detail={"ok": False, "error": "cannot import name 'MarketHoliday'"},
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"health-warning", response.content)
        self.assertIn(b"silently failing", response.content)

    def test_staff_user_sees_no_banner_when_healthy(self):
        staff = User.objects.create_user(username="staffer2", password="Correct-Horse-Battery-Staple-42")
        staff.is_staff = True
        staff.save(update_fields=["is_staff"])
        self.client.login(username="staffer2", password="Correct-Horse-Battery-Staple-42")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"health-warning", response.content)


class DashboardSentimentGaugeTests(TestCase):
    def setUp(self):
        self.url = reverse("dashboard")
        self.user = User.objects.create_user(username="gaugeviewer", password="Correct-Horse-Battery-Staple-42")
        self.client.login(username="gaugeviewer", password="Correct-Horse-Battery-Staple-42")

    def test_gauge_renders_for_the_latest_snapshot_of_each_enabled_exchange(self):
        MarketSnapshot.objects.create(exchange=Exchange.DSE, as_of=timezone.localdate(), advancers=150, decliners=50, unchanged=20)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"sentiment-gauge-card", response.content)
        self.assertIn(b"Bullish", response.content)

    def test_no_gauge_when_no_snapshot_exists_yet(self):
        MarketSnapshot.objects.all().delete()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"sentiment-gauge-card", response.content)

    def test_gauge_uses_each_exchanges_own_latest_snapshot_not_a_shared_top_four(self):
        """Regression guard for the bug the naive `snapshots[:4]` query would
        have: if one exchange posts several snapshots in a row, its own
        older rows must not crowd out another enabled exchange's latest one."""
        old = timezone.localdate() - timedelta(days=5)
        for days_ago in range(5):
            MarketSnapshot.objects.create(
                exchange=Exchange.DSE,
                as_of=old + timedelta(days=days_ago),
                advancers=10,
                decliners=10,
                unchanged=0,
            )
        latest = MarketSnapshot.objects.create(
            exchange=Exchange.DSE, as_of=timezone.localdate(), advancers=0, decliners=100, unchanged=0
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Extremely Bearish", response.content)
