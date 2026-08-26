"""
scan_price_quality Celery task tests: retry policy, locking, TaskRun
recording, and per-enabled-exchange fan-out. Mirrors the conventions in
market/tests/test_reliability_task.py.
"""
from unittest import mock

from django.test import TestCase

from market.models import TaskRun, TaskStatus
from market.tasks import _TRANSIENT_ERRORS, scan_price_quality


class RetryPolicyTests(TestCase):
    def test_retries_only_transient_errors(self):
        self.assertEqual(set(scan_price_quality.autoretry_for), set(_TRANSIENT_ERRORS))

    def test_bounded_retries_and_backoff(self):
        self.assertTrue(scan_price_quality.retry_backoff)
        self.assertGreater(scan_price_quality.max_retries, 0)
        self.assertLess(scan_price_quality.max_retries, 10)

    def test_has_a_time_limit_with_soft_below_hard(self):
        self.assertIsNotNone(scan_price_quality.time_limit)
        self.assertIsNotNone(scan_price_quality.soft_time_limit)
        self.assertLess(scan_price_quality.soft_time_limit, scan_price_quality.time_limit)

    def test_programming_errors_are_not_in_the_retry_set(self):
        self.assertNotIn(ValueError, scan_price_quality.autoretry_for)
        self.assertNotIn(KeyError, scan_price_quality.autoretry_for)


class LockingIntegrationTests(TestCase):
    @mock.patch("market.services.exchange_config.enabled_exchanges", return_value=["DSE"])
    @mock.patch("market.services.data_quality.run_quality_scan", return_value={"ok": True})
    def test_acquires_the_market_write_lock(self, mock_scan, mock_exchanges):
        with mock.patch("market.services.autosync.exclusive_db_write") as mock_lock:
            mock_lock.return_value.__enter__ = mock.Mock(return_value=None)
            mock_lock.return_value.__exit__ = mock.Mock(return_value=False)
            scan_price_quality()
        mock_lock.assert_called_once()
        mock_scan.assert_called_once_with(exchange="DSE")


class FanOutTests(TestCase):
    @mock.patch("market.services.exchange_config.enabled_exchanges", return_value=["DSE", "CSE"])
    @mock.patch("market.services.data_quality.run_quality_scan")
    def test_scans_every_enabled_exchange_separately(self, mock_scan, mock_exchanges):
        mock_scan.side_effect = lambda exchange: {"ok": True, "exchange": exchange}
        result = scan_price_quality()
        self.assertEqual(mock_scan.call_count, 2)
        mock_scan.assert_any_call(exchange="DSE")
        mock_scan.assert_any_call(exchange="CSE")
        self.assertTrue(result["ok"])
        self.assertEqual(set(result["by_exchange"].keys()), {"DSE", "CSE"})

    @mock.patch("market.services.exchange_config.enabled_exchanges", return_value=["DSE"])
    @mock.patch("market.services.data_quality.run_quality_scan", return_value={"ok": False, "error": "boom"})
    def test_a_failed_exchange_scan_is_reflected_in_overall_ok(self, mock_scan, mock_exchanges):
        result = scan_price_quality()
        self.assertFalse(result["ok"])


class TaskRunRecordingTests(TestCase):
    @mock.patch("market.services.exchange_config.enabled_exchanges", return_value=["DSE"])
    @mock.patch("market.services.data_quality.run_quality_scan", return_value={"ok": True, "rows_flagged": 3})
    def test_success_is_recorded_in_task_run(self, mock_scan, mock_exchanges):
        scan_price_quality()
        run = TaskRun.objects.get(task_name="market.tasks.scan_price_quality")
        self.assertEqual(run.status, TaskStatus.SUCCESS)

    @mock.patch("market.services.exchange_config.enabled_exchanges", return_value=["DSE"])
    @mock.patch("market.services.data_quality.run_quality_scan", side_effect=RuntimeError("boom"))
    def test_failure_is_recorded_and_reraised(self, mock_scan, mock_exchanges):
        with self.assertRaises(RuntimeError):
            scan_price_quality()
        run = TaskRun.objects.get(task_name="market.tasks.scan_price_quality")
        self.assertEqual(run.status, TaskStatus.FAILURE)
        self.assertIn("boom", run.error)
