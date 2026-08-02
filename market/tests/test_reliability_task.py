"""
ML Reliability Monitor — Celery task tests: retry policy, locking,
TaskRun recording, and idempotent repeated execution. Mirrors the
conventions in market/tests/test_task_idempotency.py.
"""
from unittest import mock

from django.test import TestCase

from market.models import TaskRun, TaskStatus
from market.tasks import _TRANSIENT_ERRORS, assess_ml_reliability


class RetryPolicyTests(TestCase):
    def test_retries_only_transient_errors(self):
        self.assertEqual(set(assess_ml_reliability.autoretry_for), set(_TRANSIENT_ERRORS))

    def test_bounded_retries_and_backoff(self):
        self.assertTrue(assess_ml_reliability.retry_backoff)
        self.assertGreater(assess_ml_reliability.max_retries, 0)
        self.assertLess(assess_ml_reliability.max_retries, 10)

    def test_has_a_time_limit_with_soft_below_hard(self):
        self.assertIsNotNone(assess_ml_reliability.time_limit)
        self.assertIsNotNone(assess_ml_reliability.soft_time_limit)
        self.assertLess(assess_ml_reliability.soft_time_limit, assess_ml_reliability.time_limit)

    def test_programming_errors_are_not_in_the_retry_set(self):
        self.assertNotIn(ValueError, assess_ml_reliability.autoretry_for)
        self.assertNotIn(KeyError, assess_ml_reliability.autoretry_for)


class LockingIntegrationTests(TestCase):
    @mock.patch("market.services.reliability_report.run_reliability_assessment", return_value={"ok": True})
    def test_acquires_the_market_write_lock(self, mock_run):
        with mock.patch("market.services.autosync.exclusive_db_write") as mock_lock:
            mock_lock.return_value.__enter__ = mock.Mock(return_value=None)
            mock_lock.return_value.__exit__ = mock.Mock(return_value=False)
            assess_ml_reliability()
        mock_lock.assert_called_once()
        mock_run.assert_called_once()

    def test_duplicate_invocation_is_rejected_while_lock_held(self):
        from market.services.autosync import exclusive_db_write
        from market.services.locking import distributed_lock

        with distributed_lock("market-write", timeout=10, blocking_timeout=0):
            with self.assertRaises(TimeoutError):
                with exclusive_db_write(blocking=False):
                    pass  # pragma: no cover — a duplicate run must never reach here


class TaskRunRecordingTests(TestCase):
    @mock.patch("market.services.reliability_report.run_reliability_assessment", return_value={"ok": True, "assessments": []})
    def test_success_is_recorded_in_task_run(self, mock_run):
        assess_ml_reliability()
        run = TaskRun.objects.get(task_name="market.tasks.assess_ml_reliability")
        self.assertEqual(run.status, TaskStatus.SUCCESS)
        self.assertEqual(run.detail, {"ok": True, "assessments": []})

    @mock.patch("market.services.reliability_report.run_reliability_assessment", side_effect=RuntimeError("boom"))
    def test_failure_is_recorded_and_reraised(self, mock_run):
        with self.assertRaises(RuntimeError):
            assess_ml_reliability()
        run = TaskRun.objects.get(task_name="market.tasks.assess_ml_reliability")
        self.assertEqual(run.status, TaskStatus.FAILURE)
        self.assertIn("boom", run.error)


class TaskCallsOrchestratorWithNoArgumentsTests(TestCase):
    """The scheduled task always runs the full default sweep (all
    families/exchanges/windows) — no partial/filtered runs from beat."""

    @mock.patch("market.services.reliability_report.run_reliability_assessment", return_value={"ok": True})
    def test_task_calls_orchestrator_with_defaults(self, mock_run):
        assess_ml_reliability()
        mock_run.assert_called_once_with()
