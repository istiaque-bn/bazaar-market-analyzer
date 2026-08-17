"""Phase 9 — alert threshold evaluation (market.services.ops_alerts)."""
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from market.models import TaskRun, TaskStatus
from market.services.ops_alerts import (
    FETCH_TASK_NAMES,
    REPEATED_FAILURE_STREAK,
    STALE_DATA_DAYS,
    STUCK_JOB_MINUTES,
    evaluate_alerts,
)


class StaleDataAlertTests(TestCase):
    def test_fresh_data_raises_no_alert(self):
        from market.services.ops_alerts import _stale_data_alerts

        today = timezone.localdate().isoformat()
        alerts = _stale_data_alerts({"DSE": {"latest_price_date": today}})
        self.assertEqual(alerts, [])

    def test_data_older_than_threshold_raises_warning(self):
        from market.services.ops_alerts import _stale_data_alerts

        old = (timezone.localdate() - timedelta(days=STALE_DATA_DAYS + 1)).isoformat()
        alerts = _stale_data_alerts({"DSE": {"latest_price_date": old}})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["key"], "stale_data_DSE")
        self.assertEqual(alerts[0]["severity"], "warning")

    def test_no_data_at_all_raises_critical(self):
        from market.services.ops_alerts import _stale_data_alerts

        alerts = _stale_data_alerts({"CSE": {"latest_price_date": None}})
        self.assertEqual(alerts[0]["severity"], "critical")


class StaleAnalysisAlertTests(TestCase):
    def test_fresh_analysis_raises_no_alert(self):
        from market.services.ops_alerts import _stale_analysis_alerts

        today = timezone.localdate().isoformat()
        self.assertEqual(_stale_analysis_alerts({"latest_as_of": today}), [])

    def test_stale_analysis_raises_warning(self):
        from market.services.ops_alerts import _stale_analysis_alerts

        old = (timezone.localdate() - timedelta(days=STALE_DATA_DAYS + 1)).isoformat()
        alerts = _stale_analysis_alerts({"latest_as_of": old})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["key"], "stale_analysis")
        self.assertEqual(alerts[0]["severity"], "warning")

    def test_very_stale_analysis_raises_critical(self):
        from market.services.ops_alerts import _stale_analysis_alerts

        old = (timezone.localdate() - timedelta(days=STALE_DATA_DAYS * 2 + 1)).isoformat()
        alerts = _stale_analysis_alerts({"latest_as_of": old})
        self.assertEqual(alerts[0]["severity"], "critical")

    def test_no_analysis_ever_raises_critical(self):
        from market.services.ops_alerts import _stale_analysis_alerts

        alerts = _stale_analysis_alerts({"latest_as_of": None})
        self.assertEqual(alerts[0]["severity"], "critical")
        self.assertEqual(alerts[0]["key"], "stale_analysis")


class SilentSyncFailureAlertTests(TestCase):
    def test_no_runs_raises_nothing(self):
        from market.services.ops_alerts import _silent_sync_failure_alerts

        self.assertEqual(_silent_sync_failure_alerts(), [])

    def test_successful_run_with_no_embedded_error_raises_nothing(self):
        from market.services.ops_alerts import _silent_sync_failure_alerts

        TaskRun.objects.create(
            task_name=FETCH_TASK_NAMES[0],
            status=TaskStatus.SUCCESS,
            detail={"ok": True, "dse": {"ok": True, "count": 10}, "cse": {"ok": True, "count": 8}},
        )
        self.assertEqual(_silent_sync_failure_alerts(), [])

    def test_success_status_with_embedded_error_raises_critical(self):
        """The exact bug this alert exists to catch: the Celery task
        itself didn't raise (status=SUCCESS), but the result it returned
        recorded a real failure — e.g. autosync's own try/except caught
        an ImportError and returned it as {"ok": False, "error": ...}."""
        from market.services.ops_alerts import _silent_sync_failure_alerts

        TaskRun.objects.create(
            task_name=FETCH_TASK_NAMES[0],
            status=TaskStatus.SUCCESS,
            detail={"ok": False, "error": "cannot import name 'MarketHoliday'"},
        )
        alerts = _silent_sync_failure_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["severity"], "critical")
        self.assertIn("MarketHoliday", alerts[0]["message"])

    def test_nested_last_error_in_skipped_fresh_shape_is_caught(self):
        from market.services.ops_alerts import _silent_sync_failure_alerts

        TaskRun.objects.create(
            task_name=FETCH_TASK_NAMES[0],
            status=TaskStatus.SUCCESS,
            detail={"ok": True, "skipped": "fresh", "last_error": "SSL: CERTIFICATE_VERIFY_FAILED"},
        )
        alerts = _silent_sync_failure_alerts()
        self.assertEqual(len(alerts), 1)

    def test_nested_error_under_live_key_is_caught(self):
        from market.services.ops_alerts import _silent_sync_failure_alerts

        TaskRun.objects.create(
            task_name="market.tasks.append_daily_bars",
            status=TaskStatus.SUCCESS,
            detail={"live": {"ok": False, "error": "boom"}, "analysis": None},
        )
        alerts = _silent_sync_failure_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["key"], "silent_sync_failure_market.tasks.append_daily_bars")

    def test_failure_status_run_is_not_double_counted(self):
        """A run that already raised is TaskStatus.FAILURE and is covered
        by _repeated_failure_alerts — this alert only looks at runs that
        reported success, so the two don't overlap on the same run."""
        from market.services.ops_alerts import _silent_sync_failure_alerts

        TaskRun.objects.create(task_name=FETCH_TASK_NAMES[0], status=TaskStatus.FAILURE, error="boom")
        self.assertEqual(_silent_sync_failure_alerts(), [])


class RepeatedFailureAlertTests(TestCase):
    def test_below_streak_threshold_raises_nothing(self):
        from market.services.ops_alerts import _repeated_failure_alerts

        task_name = FETCH_TASK_NAMES[0]
        for _ in range(REPEATED_FAILURE_STREAK - 1):
            TaskRun.objects.create(task_name=task_name, status=TaskStatus.FAILURE)
        self.assertEqual(_repeated_failure_alerts(), [])

    def test_full_streak_of_failures_raises_critical(self):
        from market.services.ops_alerts import _repeated_failure_alerts

        task_name = FETCH_TASK_NAMES[0]
        for _ in range(REPEATED_FAILURE_STREAK):
            TaskRun.objects.create(task_name=task_name, status=TaskStatus.FAILURE, error="boom")
        alerts = _repeated_failure_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["severity"], "critical")
        self.assertEqual(alerts[0]["key"], f"repeated_failure_{task_name}")

    def test_one_success_in_the_streak_clears_the_alert(self):
        from market.services.ops_alerts import _repeated_failure_alerts

        task_name = FETCH_TASK_NAMES[0]
        TaskRun.objects.create(task_name=task_name, status=TaskStatus.FAILURE)
        TaskRun.objects.create(task_name=task_name, status=TaskStatus.SUCCESS)
        TaskRun.objects.create(task_name=task_name, status=TaskStatus.FAILURE)
        self.assertEqual(_repeated_failure_alerts(), [])


class JobOverlapAndStuckAlertTests(TestCase):
    def test_two_concurrent_started_runs_raise_overlap_warning(self):
        from market.services.ops_alerts import _job_overlap_and_stuck_alerts

        TaskRun.objects.create(task_name="market.tasks.run_full_analysis", status=TaskStatus.STARTED)
        TaskRun.objects.create(task_name="market.tasks.run_full_analysis", status=TaskStatus.STARTED)
        alerts = _job_overlap_and_stuck_alerts()
        keys = {a["key"] for a in alerts}
        self.assertIn("job_overlap_market.tasks.run_full_analysis", keys)

    def test_single_recent_started_run_is_not_stuck(self):
        from market.services.ops_alerts import _job_overlap_and_stuck_alerts

        TaskRun.objects.create(task_name="market.tasks.train_ml_model", status=TaskStatus.STARTED)
        alerts = _job_overlap_and_stuck_alerts()
        self.assertEqual(alerts, [])

    def test_old_started_run_raises_stuck_critical(self):
        from market.services.ops_alerts import _job_overlap_and_stuck_alerts

        run = TaskRun.objects.create(task_name="market.tasks.train_ml_model", status=TaskStatus.STARTED)
        run.started_at = timezone.now() - timedelta(minutes=STUCK_JOB_MINUTES + 5)
        run.save(update_fields=["started_at"])
        alerts = _job_overlap_and_stuck_alerts()
        stuck = [a for a in alerts if a["key"] == "stuck_job_market.tasks.train_ml_model"]
        self.assertEqual(len(stuck), 1)
        self.assertEqual(stuck[0]["severity"], "critical")


class DatabaseAlertTests(TestCase):
    def test_reachable_database_raises_nothing(self):
        from market.services.ops_alerts import _database_alerts

        self.assertEqual(_database_alerts(), [])

    def test_unreachable_database_raises_critical_with_no_exception_detail(self):
        from market.services.ops_alerts import _database_alerts

        with mock.patch("market.services.ops_alerts.check_database", return_value=False):
            alerts = _database_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["severity"], "critical")
        self.assertEqual(alerts[0]["detail"], {})  # no connection string / exception text


class ModelDegradationAlertTests(TestCase):
    def test_deployed_model_with_non_positive_skill_raises_warning(self):
        from market.services.ops_alerts import _model_degradation_alerts

        models = {
            "forward_return_model": {
                "DSE": {"deployed": True, "skill_vs_naive": -0.02, "version": "v1"},
                "CSE": {"deployed": False, "skill_vs_naive": None, "version": None},
            },
            "next_close_model": {"n": 10, "skill_vs_naive": None},
        }
        alerts = _model_degradation_alerts(models)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["key"], "model_degraded_forward_return_DSE")

    def test_next_close_model_below_min_sample_raises_nothing(self):
        from market.services.ops_alerts import _model_degradation_alerts

        models = {
            "forward_return_model": {"DSE": {"deployed": False, "skill_vs_naive": None, "version": None}},
            "next_close_model": {"n": 5, "skill_vs_naive": -0.5},  # too few settled forecasts to judge
        }
        self.assertEqual(_model_degradation_alerts(models), [])

    def test_next_close_alert_formats_current_live_sample(self):
        from market.services.ops_alerts import _model_degradation_alerts

        models = {
            "forward_return_model": {"DSE": {"deployed": False, "skill_vs_naive": None, "version": None}},
            "next_close_model": {"n": 6137, "skill_vs_naive": -0.1888},
        }
        alerts = _model_degradation_alerts(models)
        self.assertEqual(len(alerts), 1)
        self.assertIn("-0.1888 over 6,137 settled forecasts", alerts[0]["message"])


class EvaluateAlertsOrderingTests(TestCase):
    def test_critical_alerts_sort_before_warnings(self):
        summary = {
            "rejected_rows": {"freshness": {"DSE": {"latest_price_date": None}}},  # critical: no data
            "predictions": {"latest_as_of": timezone.localdate().isoformat()},
            "models": {
                "forward_return_model": {"DSE": {"deployed": True, "skill_vs_naive": -0.1, "version": "v1"}},
                "next_close_model": {"n": 0, "skill_vs_naive": None},
            },
        }
        with mock.patch("market.services.ops_alerts.check_database", return_value=True):
            alerts = evaluate_alerts(summary)
        severities = [a["severity"] for a in alerts]
        self.assertEqual(severities, sorted(severities, key=lambda s: {"critical": 0, "warning": 1}.get(s, 2)))
        self.assertGreaterEqual(len(alerts), 2)
