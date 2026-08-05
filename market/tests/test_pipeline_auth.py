from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

PASSWORD = "Correct-Horse-Battery-Staple-42"


class PipelineAuthTests(TestCase):
    """Fetch/analysis/training/pipeline jobs are Admin-only ("Manage DSE
    pipeline and training controls" is an Admin capability, not Staff's —
    see accounts/roles.py), POST-only, CSRF-protected, and enqueued (not
    run synchronously on the request thread)."""

    def setUp(self):
        self.url = reverse("run_pipeline")

    def test_anonymous_cannot_trigger_pipeline(self):
        response = self.client.post(self.url, {"mode": "analyze"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_ordinary_authenticated_user_cannot_trigger_pipeline(self):
        User.objects.create_user(username="alice", password=PASSWORD)
        self.client.login(username="alice", password=PASSWORD)
        response = self.client.post(self.url, {"mode": "analyze"})
        self.assertEqual(response.status_code, 403)

    def test_plain_staff_cannot_trigger_pipeline(self):
        """Pipeline/training triggers are Admin-only — plain Staff (not
        also a superuser) must be refused, same as a regular User."""
        staff = User.objects.create_user(username="plain_staff", password=PASSWORD, is_staff=True)
        self.client.login(username="plain_staff", password=PASSWORD)
        response = self.client.post(self.url, {"mode": "analyze"})
        self.assertEqual(response.status_code, 403)

    def _login_admin(self, username):
        admin = User.objects.create_user(username=username, password=PASSWORD, is_staff=True, is_superuser=True)
        self.client.login(username=username, password=PASSWORD)
        return admin

    @mock.patch("market.tasks.run_full_analysis_task.delay")
    def test_admin_analyze_mode_enqueues_task_not_run_inline(self, mock_delay):
        self._login_admin("admin_a")
        response = self.client.post(self.url, {"mode": "analyze"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("dashboard"))
        mock_delay.assert_called_once_with(train_ml=True)

    @mock.patch("market.tasks.seed_demo_and_analyze.delay")
    def test_admin_demo_mode_enqueues_task(self, mock_delay):
        self._login_admin("admin_demo")
        response = self.client.post(self.url, {"mode": "demo"})
        self.assertEqual(response.status_code, 302)
        mock_delay.assert_called_once()

    @mock.patch("market.tasks.run_full_analysis_task.si")
    @mock.patch("market.tasks.fetch_all_market_data.s")
    def test_admin_fetch_mode_chains_fetch_then_analysis(self, mock_fetch_sig, mock_analysis_sig):
        with mock.patch("celery.chain") as mock_chain:
            mock_chain.return_value.delay = mock.Mock()
            self._login_admin("admin_fetch")
            response = self.client.post(self.url, {"mode": "fetch"})
        self.assertEqual(response.status_code, 302)
        mock_fetch_sig.assert_called_once_with(include_history=True)
        mock_analysis_sig.assert_called_once_with(train_ml=True)
        mock_chain.return_value.delay.assert_called_once()

    @mock.patch("market.tasks.run_full_analysis_task.delay")
    def test_enqueue_failure_shows_error_not_500(self, mock_delay):
        mock_delay.side_effect = RuntimeError("broker unreachable")
        self._login_admin("admin_err")
        response = self.client.post(self.url, {"mode": "analyze"})
        self.assertEqual(response.status_code, 302)  # still redirects, no 500

    def test_get_not_allowed(self):
        self._login_admin("admin_get")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)

    @mock.patch("market.tasks.run_full_analysis_task.delay")
    def test_csrf_is_enforced_for_admin_post(self, mock_delay):
        User.objects.create_user(username="admin_csrf", password=PASSWORD, is_staff=True, is_superuser=True)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.login(username="admin_csrf", password=PASSWORD)
        response = csrf_client.post(self.url, {"mode": "analyze"})
        self.assertEqual(response.status_code, 403)
        mock_delay.assert_not_called()
