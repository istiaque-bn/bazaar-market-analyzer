"""Hybrid automation system tests: settings validation, scheduling
eligibility, lightweight intraday analysis, task dedup/locking (Skipped
not Failed), ML-training gating, manual admin controls, and the new
operational alerts. Mocks every external DSE call; no wall-clock/
internet dependency."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from market.models import Exchange, Stock, TaskRun, TaskStatus
from market.services.locking import distributed_lock

PASSWORD = "Correct-Horse-Battery-Staple-42"


def make_admin(username="auto_admin") -> User:
    return User.objects.create_user(username=username, password=PASSWORD, is_staff=True, is_superuser=True)


def make_staff(username="auto_staff") -> User:
    return User.objects.create_user(username=username, password=PASSWORD, is_staff=True)


def make_user(username="auto_user") -> User:
    return User.objects.create_user(username=username, password=PASSWORD)


# ---------------------------------------------------------------------------
# Settings validation
# ---------------------------------------------------------------------------


class SettingsValidationTests(TestCase):
    def test_zero_interval_rejected(self):
        with mock.patch.dict("os.environ", {"AUTO_SYNC_INTERVAL_MARKET": "0"}):
            with self.assertRaises(ImproperlyConfigured):
                import importlib

                import config.settings.base as base_settings

                importlib.reload(base_settings)

    def test_negative_interval_rejected(self):
        with mock.patch.dict("os.environ", {"AUTO_INTRADAY_ANALYSIS_INTERVAL": "-5"}):
            with self.assertRaises(ImproperlyConfigured):
                import importlib

                import config.settings.base as base_settings

                importlib.reload(base_settings)

    def test_non_numeric_interval_rejected(self):
        with mock.patch.dict("os.environ", {"AUTO_SYNC_INTERVAL_OFF": "soon"}):
            with self.assertRaises(ImproperlyConfigured):
                import importlib

                import config.settings.base as base_settings

                importlib.reload(base_settings)

    def tearDown(self):
        # Restore the module to its normal (valid-env) state for every
        # later test in the suite, since the tests above intentionally
        # reload it with a broken environment.
        import importlib

        import config.settings.base as base_settings

        importlib.reload(base_settings)


class BeatScheduleTests(TestCase):
    def test_beat_schedule_uses_asia_dhaka_timezone(self):
        from django.conf import settings

        self.assertEqual(settings.CELERY_TIMEZONE, "Asia/Dhaka")

    def test_daily_ml_training_entry_present_when_enabled(self):
        with override_settings(AUTO_ML_TRAINING=True, AUTO_ML_TRAINING_TIME="00:30"):
            import importlib

            import config.celery as celery_module

            importlib.reload(celery_module)
            entry = celery_module.app.conf.beat_schedule.get("train-ml-model-daily")
            self.assertIsNotNone(entry)
            self.assertEqual(entry["task"], "market.tasks.train_ml_model")
            self.assertEqual(entry["schedule"].hour, {0})
            self.assertEqual(entry["schedule"].minute, {30})
            # No day-of-week restriction — this is now a daily entry.
            self.assertEqual(entry["schedule"].day_of_week, set(range(7)))

    def test_no_daily_ml_training_entry_when_disabled(self):
        with override_settings(AUTO_ML_TRAINING=False):
            import importlib

            import config.celery as celery_module

            importlib.reload(celery_module)
            self.assertNotIn("train-ml-model-daily", celery_module.app.conf.beat_schedule)

        # restore normal state for later tests
        import importlib

        import config.celery as celery_module

        importlib.reload(celery_module)


# ---------------------------------------------------------------------------
# Intraday (lightweight) analysis
# ---------------------------------------------------------------------------


class IntradayAnalysisEligibilityTests(TestCase):
    def setUp(self):
        self.stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="INTRA1", company_name="Intraday Co", is_active=True)

    def test_disabled_flag_skips(self):
        from market.services.intraday_analysis import maybe_run_intraday_analysis

        with override_settings(AUTO_INTRADAY_ANALYSIS=False):
            result = maybe_run_intraday_analysis()
        self.assertEqual(result, {"ok": True, "skipped": "disabled"})

    def test_market_closed_skips(self):
        from market.services.intraday_analysis import maybe_run_intraday_analysis

        with mock.patch("market.services.intraday_analysis.is_market_hours", return_value=False):
            result = maybe_run_intraday_analysis()
        self.assertEqual(result["skipped"], "market_closed")

    def test_no_new_data_skips_even_during_market_hours(self):
        from market.services.intraday_analysis import maybe_run_intraday_analysis

        with mock.patch("market.services.intraday_analysis.is_market_hours", return_value=True), \
                mock.patch("market.services.intraday_analysis._has_new_data_since", return_value=False):
            result = maybe_run_intraday_analysis()
        self.assertEqual(result["skipped"], "no_new_data")

    def test_interval_not_elapsed_skips(self):
        from market.services.intraday_analysis import maybe_run_intraday_analysis

        recent = timezone.now() - timedelta(seconds=10)
        with mock.patch("market.services.intraday_analysis.is_market_hours", return_value=True), \
                mock.patch("market.services.intraday_analysis._last_run_at", return_value=recent), \
                override_settings(AUTO_INTRADAY_ANALYSIS_INTERVAL=900):
            result = maybe_run_intraday_analysis()
        self.assertEqual(result["skipped"], "interval_not_elapsed")

    def test_force_bypasses_market_hours_and_interval_checks(self):
        from market.models import PriceHistory
        from market.services.intraday_analysis import maybe_run_intraday_analysis

        PriceHistory.objects.create(stock=self.stock, date=timezone.localdate(), open=10, high=11, low=9, close=10, volume=100)
        with mock.patch("market.services.intraday_analysis.is_market_hours", return_value=False):
            result = maybe_run_intraday_analysis(force=True)
        self.assertTrue(result.get("ok"))
        self.assertNotIn("skipped", result)

    def test_already_running_is_skipped_not_failed(self):
        from market.services.intraday_analysis import maybe_run_intraday_analysis

        with distributed_lock("intraday-analysis", timeout=10, blocking_timeout=0):
            with mock.patch("market.services.intraday_analysis.is_market_hours", return_value=True), \
                    mock.patch("market.services.intraday_analysis._has_new_data_since", return_value=True), \
                    mock.patch("market.services.intraday_analysis._last_run_at", return_value=None):
                result = maybe_run_intraday_analysis()
        self.assertEqual(result, {"ok": True, "skipped": "already_running"})

    def test_only_updates_technical_snapshot_not_analysis_result(self):
        """Lightweight analysis must never create/duplicate AnalysisResult
        rows — that's exclusively the full-analysis pipeline's job."""
        from market.models import AnalysisResult, PriceHistory, TechnicalSnapshot
        from market.services.intraday_analysis import run_intraday_analysis_unlocked

        for i in range(5):
            PriceHistory.objects.create(
                stock=self.stock, date=timezone.localdate() - timedelta(days=4 - i),
                open=10, high=11, low=9, close=10 + i, volume=100,
            )
        before_analysis_count = AnalysisResult.objects.count()
        result = run_intraday_analysis_unlocked()
        self.assertEqual(result["updated"], 1)
        self.assertTrue(TechnicalSnapshot.objects.filter(stock=self.stock).exists())
        self.assertEqual(AnalysisResult.objects.count(), before_analysis_count)


# ---------------------------------------------------------------------------
# Task dedup / locking (Skipped, not Failed)
# ---------------------------------------------------------------------------


class TaskDedupTests(TestCase):
    def test_duplicate_full_analysis_is_skipped(self):
        from market.tasks import run_full_analysis_task

        with distributed_lock("full-analysis", timeout=10, blocking_timeout=0):
            result = run_full_analysis_task()
        self.assertEqual(result, {"ok": True, "skipped": "already_running"})

    def test_duplicate_ml_training_is_skipped(self):
        from market.tasks import train_ml_model

        with distributed_lock("ml-training", timeout=10, blocking_timeout=0):
            result = train_ml_model()
        self.assertEqual(result, {"ok": True, "skipped": "already_running"})

    def test_skipped_result_recorded_as_skipped_status_not_success(self):
        from market.services.task_status import record_task_run

        @record_task_run("test.skip_task")
        def skip():
            return {"ok": True, "skipped": "disabled"}

        skip()
        run = TaskRun.objects.get(task_name="test.skip_task")
        self.assertEqual(run.status, TaskStatus.SKIPPED)

    def test_normal_success_still_recorded_as_success(self):
        from market.services.task_status import record_task_run

        @record_task_run("test.ok_task")
        def ok():
            return {"ok": True, "count": 3}

        ok()
        run = TaskRun.objects.get(task_name="test.ok_task")
        self.assertEqual(run.status, TaskStatus.SUCCESS)


class AutomationToggleTests(TestCase):
    def test_ml_training_disabled_flag_short_circuits_task(self):
        from market.tasks import train_ml_model

        with override_settings(AUTO_ML_TRAINING=False):
            result = train_ml_model()
        self.assertEqual(result, {"ok": True, "skipped": "disabled"})

    def test_daily_append_disabled_flag_short_circuits(self):
        from market.services.daily_append import run_scheduled_append

        with override_settings(AUTO_DAILY_APPEND=False):
            result = run_scheduled_append()
        self.assertEqual(result, {"ok": True, "skipped": "disabled"})

    def test_close_learn_disabled_flag_short_circuits_task(self):
        from market.tasks import close_learn_settlement

        with override_settings(AUTO_CLOSE_LEARN=False):
            result = close_learn_settlement()
        self.assertEqual(result, {"ok": True, "skipped": "disabled"})

    @override_settings(ENABLE_DSE=True, ENABLE_CSE=False)
    def test_ml_training_excludes_cse_when_disabled(self):
        """market.services.ml_model.train_model already restricts panels
        to enabled_exchanges() (see market/tests/test_exchange_config.py)
        — this just confirms the task-level wrapper doesn't bypass it."""
        with mock.patch("market.services.ml_model.build_training_panel") as mock_build:
            import pandas as pd

            mock_build.return_value = pd.DataFrame()
            from market.tasks import train_ml_model

            train_ml_model()
            called_exchanges = {c.args[0] for c in mock_build.call_args_list}
        self.assertNotIn(Exchange.CSE, called_exchanges)


# ---------------------------------------------------------------------------
# End-of-day pipeline
# ---------------------------------------------------------------------------


class EndOfDayPipelineTests(TestCase):
    def test_pipeline_runs_all_stages_and_records_each_independently(self):
        from market.tasks import run_end_of_day_pipeline

        with mock.patch("market.tasks.append_daily_bars", return_value={"ok": True}) as m_append, \
                mock.patch("market.tasks.close_learn_settlement", return_value={"ok": True}) as m_close, \
                mock.patch("market.tasks.assess_ml_reliability", return_value={"ok": True}) as m_rel, \
                mock.patch("notifications.tasks.send_daily_digest", return_value={"ok": True}) as m_digest:
            result = run_end_of_day_pipeline()
        m_append.assert_called_once()
        m_close.assert_called_once()
        m_rel.assert_called_once()
        m_digest.assert_called_once()
        self.assertTrue(result["ok"])
        self.assertEqual(set(result["stages"]), {"append", "close_learn_settlement", "reliability_assessment", "digest"})

    def test_one_failed_stage_does_not_mark_pipeline_ok_but_others_still_run(self):
        from market.tasks import run_end_of_day_pipeline

        with mock.patch("market.tasks.append_daily_bars", side_effect=RuntimeError("boom")), \
                mock.patch("market.tasks.close_learn_settlement", return_value={"ok": True}) as m_close, \
                mock.patch("market.tasks.assess_ml_reliability", return_value={"ok": True}) as m_rel, \
                mock.patch("notifications.tasks.send_daily_digest", return_value={"ok": True}) as m_digest:
            result = run_end_of_day_pipeline()
        self.assertFalse(result["ok"])
        self.assertFalse(result["stages"]["append"]["ok"])
        m_close.assert_called_once()
        m_rel.assert_called_once()
        m_digest.assert_called_once()


# ---------------------------------------------------------------------------
# Manual admin controls / authorization
# ---------------------------------------------------------------------------


class ManualControlAuthorizationTests(TestCase):
    def setUp(self):
        self.url = reverse("run_pipeline")

    def test_admin_can_enqueue_intraday_analysis(self):
        make_admin("mc_admin1")
        self.client.login(username="mc_admin1", password=PASSWORD)
        with mock.patch("market.tasks.run_intraday_analysis.delay") as mock_delay:
            response = self.client.post(self.url, {"mode": "intraday"})
        self.assertEqual(response.status_code, 302)
        mock_delay.assert_called_once()

    def test_admin_can_enqueue_end_of_day_pipeline(self):
        make_admin("mc_admin2")
        self.client.login(username="mc_admin2", password=PASSWORD)
        with mock.patch("market.tasks.run_end_of_day_pipeline.delay") as mock_delay:
            response = self.client.post(self.url, {"mode": "eod"})
        self.assertEqual(response.status_code, 302)
        mock_delay.assert_called_once()

    def test_training_requires_confirmation(self):
        make_admin("mc_admin3")
        self.client.login(username="mc_admin3", password=PASSWORD)
        with mock.patch("market.tasks.train_ml_model.delay") as mock_delay:
            response = self.client.post(self.url, {"mode": "train"})
        self.assertEqual(response.status_code, 302)
        mock_delay.assert_not_called()

    def test_training_with_confirmation_enqueues(self):
        make_admin("mc_admin4")
        self.client.login(username="mc_admin4", password=PASSWORD)
        with mock.patch("market.tasks.train_ml_model.delay") as mock_delay:
            response = self.client.post(self.url, {"mode": "train", "confirm": "yes"})
        self.assertEqual(response.status_code, 302)
        mock_delay.assert_called_once()

    def test_staff_cannot_enqueue_any_pipeline_mode(self):
        make_staff("mc_staff1")
        self.client.login(username="mc_staff1", password=PASSWORD)
        for mode in ("quote", "intraday", "analyze", "eod", "train"):
            with self.subTest(mode=mode):
                response = self.client.post(self.url, {"mode": mode, "confirm": "yes"})
                self.assertEqual(response.status_code, 403)

    def test_regular_user_cannot_enqueue_any_pipeline_mode(self):
        make_user("mc_user1")
        self.client.login(username="mc_user1", password=PASSWORD)
        response = self.client.post(self.url, {"mode": "eod"})
        self.assertEqual(response.status_code, 403)

    def test_get_request_not_allowed(self):
        make_admin("mc_admin5")
        self.client.login(username="mc_admin5", password=PASSWORD)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)


class RetryTaskViewTests(TestCase):
    def setUp(self):
        self.admin = make_admin("retry_admin")
        self.client.login(username="retry_admin", password=PASSWORD)

    def test_admin_can_retry_a_failed_allow_listed_task(self):
        run = TaskRun.objects.create(task_name="market.tasks.sync_live_market", status=TaskStatus.FAILURE, error="boom")
        with mock.patch("market.tasks.sync_live_market.delay") as mock_delay:
            response = self.client.post(reverse("retry_task", args=[run.id]))
        self.assertEqual(response.status_code, 302)
        mock_delay.assert_called_once()

    def test_cannot_retry_a_successful_run(self):
        run = TaskRun.objects.create(task_name="market.tasks.sync_live_market", status=TaskStatus.SUCCESS)
        with mock.patch("market.tasks.sync_live_market.delay") as mock_delay:
            response = self.client.post(reverse("retry_task", args=[run.id]))
        self.assertEqual(response.status_code, 302)
        mock_delay.assert_not_called()

    def test_cannot_retry_a_non_allow_listed_task_name(self):
        run = TaskRun.objects.create(task_name="some.unknown.task", status=TaskStatus.FAILURE, error="boom")
        response = self.client.post(reverse("retry_task", args=[run.id]))
        self.assertEqual(response.status_code, 302)  # redirects with an error message, doesn't crash

    def test_staff_cannot_retry_tasks(self):
        make_staff("retry_staff")
        self.client.logout()
        self.client.login(username="retry_staff", password=PASSWORD)
        run = TaskRun.objects.create(task_name="market.tasks.sync_live_market", status=TaskStatus.FAILURE, error="boom")
        response = self.client.post(reverse("retry_task", args=[run.id]))
        self.assertEqual(response.status_code, 403)

    def test_retry_requires_post(self):
        run = TaskRun.objects.create(task_name="market.tasks.sync_live_market", status=TaskStatus.FAILURE, error="boom")
        response = self.client.get(reverse("retry_task", args=[run.id]))
        self.assertEqual(response.status_code, 405)


# ---------------------------------------------------------------------------
# Operational alerts (new: missing append, worker absence, backlog)
# ---------------------------------------------------------------------------


class MissingDailyAppendAlertTests(TestCase):
    def test_no_alert_when_disabled(self):
        from market.services.ops_alerts import _missing_daily_append_alert

        with override_settings(AUTO_DAILY_APPEND=False):
            self.assertEqual(_missing_daily_append_alert(), [])

    def test_no_alert_before_settlement_hour(self):
        from market.services.ops_alerts import _missing_daily_append_alert

        early = timezone.now().replace(hour=9, minute=0)
        with mock.patch("market.services.ops_alerts.timezone.localtime", return_value=early):
            self.assertEqual(_missing_daily_append_alert(), [])

    def test_alert_fires_when_missing_past_settlement_hour_on_trading_day(self):
        from market.services.ops_alerts import _missing_daily_append_alert

        late = timezone.now().replace(hour=16, minute=0)
        with mock.patch("market.services.ops_alerts.timezone.localtime", return_value=late), \
                mock.patch("market.services.trading_calendar.closure_reason", return_value=None):
            alerts = _missing_daily_append_alert()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["key"], "missing_daily_append")

    def test_no_alert_on_a_holiday(self):
        from market.services.ops_alerts import _missing_daily_append_alert

        late = timezone.now().replace(hour=16, minute=0)
        with mock.patch("market.services.ops_alerts.timezone.localtime", return_value=late), \
                mock.patch("market.services.trading_calendar.closure_reason", return_value="Weekend"):
            self.assertEqual(_missing_daily_append_alert(), [])

    def test_no_alert_when_a_successful_append_already_ran_today(self):
        from market.services.ops_alerts import _missing_daily_append_alert

        TaskRun.objects.create(task_name="market.tasks.append_daily_bars", status=TaskStatus.SUCCESS)
        late = timezone.now().replace(hour=16, minute=0)
        with mock.patch("market.services.ops_alerts.timezone.localtime", return_value=late), \
                mock.patch("market.services.trading_calendar.closure_reason", return_value=None):
            self.assertEqual(_missing_daily_append_alert(), [])


class WorkerAbsenceAlertTests(TestCase):
    def test_no_alert_when_market_closed(self):
        from market.services.ops_alerts import _worker_absence_alert

        with mock.patch("market.services.autosync.is_market_hours", return_value=False):
            self.assertEqual(_worker_absence_alert(), [])

    def test_alert_fires_when_no_recent_sync_task_during_market_hours(self):
        from market.services.ops_alerts import _worker_absence_alert

        with mock.patch("market.services.autosync.is_market_hours", return_value=True):
            alerts = _worker_absence_alert()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["key"], "worker_absent")

    def test_no_alert_when_sync_task_ran_recently(self):
        from market.services.ops_alerts import _worker_absence_alert

        TaskRun.objects.create(task_name="market.tasks.sync_live_market", status=TaskStatus.SUCCESS)
        with mock.patch("market.services.autosync.is_market_hours", return_value=True):
            self.assertEqual(_worker_absence_alert(), [])


class TaskBacklogAlertTests(TestCase):
    def test_no_alert_when_broker_unreachable(self):
        from market.services.ops_alerts import _task_backlog_alert

        with mock.patch("redis.Redis.from_url", side_effect=Exception("no broker")):
            self.assertEqual(_task_backlog_alert(), [])

    def test_alert_fires_above_threshold(self):
        from market.services.ops_alerts import TASK_BACKLOG_THRESHOLD, _task_backlog_alert

        mock_client = mock.Mock()
        mock_client.llen.return_value = TASK_BACKLOG_THRESHOLD + 1
        with mock.patch("redis.Redis.from_url", return_value=mock_client):
            alerts = _task_backlog_alert()
        self.assertEqual(alerts[0]["key"], "task_backlog")

    def test_no_alert_below_threshold(self):
        from market.services.ops_alerts import _task_backlog_alert

        mock_client = mock.Mock()
        mock_client.llen.return_value = 1
        with mock.patch("redis.Redis.from_url", return_value=mock_client):
            self.assertEqual(_task_backlog_alert(), [])


class ClosedMarketNoFalseAlertsTests(TestCase):
    """Disabled/closed conditions must never themselves generate alerts."""

    def test_evaluate_alerts_has_no_worker_absence_when_market_closed(self):
        from market.services.ops_alerts import evaluate_alerts
        from market.services.ops_metrics import ops_summary

        with mock.patch("market.services.autosync.is_market_hours", return_value=False), \
                mock.patch("market.services.ops_alerts.check_database", return_value=True):
            alerts = evaluate_alerts(ops_summary())
        self.assertNotIn("worker_absent", {a["key"] for a in alerts})

    @override_settings(ENABLE_DSE=True, ENABLE_CSE=False)
    def test_disabled_cse_freshness_never_alerts(self):
        from market.services.ops_alerts import _stale_data_alerts

        alerts = _stale_data_alerts({"CSE": {"latest_price_date": None, "enabled": False}})
        self.assertEqual(alerts, [])
