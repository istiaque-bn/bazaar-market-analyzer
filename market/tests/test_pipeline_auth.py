from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse


class PipelineAuthTests(TestCase):
    """Fetch/analysis/training/pipeline jobs must be staff-only, POST-only,
    CSRF-protected, and enqueued (not run synchronously on the request
    thread) — ordinary or anonymous users must not be able to trigger
    them (they hit external upstreams and retrain the ML model)."""

    def setUp(self):
        self.url = reverse("run_pipeline")

    def test_anonymous_cannot_trigger_pipeline(self):
        response = self.client.post(self.url, {"mode": "analyze"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)

    def test_ordinary_authenticated_user_cannot_trigger_pipeline(self):
        User.objects.create_user(username="alice", password="Correct-Horse-Battery-Staple-42")
        self.client.login(username="alice", password="Correct-Horse-Battery-Staple-42")
        response = self.client.post(self.url, {"mode": "analyze"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)

    def _login_staff(self, username):
        staff = User.objects.create_user(username=username, password="Correct-Horse-Battery-Staple-42")
        staff.is_staff = True
        staff.save(update_fields=["is_staff"])
        self.client.login(username=username, password="Correct-Horse-Battery-Staple-42")
        return staff

    @mock.patch("market.tasks.run_full_analysis_task.delay")
    def test_staff_analyze_mode_enqueues_task_not_run_inline(self, mock_delay):
        self._login_staff("staffer")
        response = self.client.post(self.url, {"mode": "analyze"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("dashboard"))
        mock_delay.assert_called_once_with(train_ml=True)

    @mock.patch("market.tasks.seed_demo_and_analyze.delay")
    def test_staff_demo_mode_enqueues_task(self, mock_delay):
        self._login_staff("staffer_demo")
        response = self.client.post(self.url, {"mode": "demo"})
        self.assertEqual(response.status_code, 302)
        mock_delay.assert_called_once()

    @mock.patch("market.tasks.run_full_analysis_task.si")
    @mock.patch("market.tasks.fetch_all_market_data.s")
    def test_staff_fetch_mode_chains_fetch_then_analysis(self, mock_fetch_sig, mock_analysis_sig):
        with mock.patch("celery.chain") as mock_chain:
            mock_chain.return_value.delay = mock.Mock()
            self._login_staff("staffer_fetch")
            response = self.client.post(self.url, {"mode": "fetch"})
        self.assertEqual(response.status_code, 302)
        mock_fetch_sig.assert_called_once_with(include_history=True)
        mock_analysis_sig.assert_called_once_with(train_ml=True)
        mock_chain.return_value.delay.assert_called_once()

    @mock.patch("market.tasks.run_full_analysis_task.delay")
    def test_enqueue_failure_shows_error_not_500(self, mock_delay):
        mock_delay.side_effect = RuntimeError("broker unreachable")
        self._login_staff("staffer_err")
        response = self.client.post(self.url, {"mode": "analyze"})
        self.assertEqual(response.status_code, 302)  # still redirects, no 500

    def test_get_not_allowed(self):
        self._login_staff("staffer2")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)

    @mock.patch("market.tasks.run_full_analysis_task.delay")
    def test_csrf_is_enforced_for_staff_post(self, mock_delay):
        staff = User.objects.create_user(username="staffer3", password="Correct-Horse-Battery-Staple-42")
        staff.is_staff = True
        staff.save(update_fields=["is_staff"])
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.login(username="staffer3", password="Correct-Horse-Battery-Staple-42")
        response = csrf_client.post(self.url, {"mode": "analyze"})
        self.assertEqual(response.status_code, 403)
        mock_delay.assert_not_called()
