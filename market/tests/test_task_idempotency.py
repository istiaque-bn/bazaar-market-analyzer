"""Celery task idempotency, locking, and failure-recovery tests.

Real ML training / network calls are always mocked here — these are unit
tests of task orchestration (retry policy, locking, status recording,
idempotent re-runs), not integration tests of the underlying fetch/ML
pipelines (covered separately), and must never touch the real
data/cache/*.pkl model files or make network calls.
"""
from datetime import date, timedelta
from unittest import mock

import requests
from celery import shared_task
from django.db.utils import OperationalError
from django.test import TestCase
from django.utils import timezone

from market.models import Exchange, PriceHistory, Stock, TaskRun, TaskStatus
from market.services.task_status import ORPHAN_GRACE_SECONDS, record_task_run, reconcile_orphaned_task_runs
from market.tasks import (
    _TRANSIENT_ERRORS,
    append_daily_bars,
    close_learn_settlement,
    fetch_all_market_data,
    run_full_analysis_task,
    sync_live_market,
    train_ml_model,
)


class RetryPolicyConfigTests(TestCase):
    """Only transient failures (lock busy, DB locked, network) may be
    retried automatically — logic bugs must surface as real failures."""

    TASKS = [
        sync_live_market,
        fetch_all_market_data,
        run_full_analysis_task,
        append_daily_bars,
        train_ml_model,
        close_learn_settlement,
    ]

    def test_transient_errors_include_timeout_dblock_and_network(self):
        self.assertIn(TimeoutError, _TRANSIENT_ERRORS)
        self.assertIn(OperationalError, _TRANSIENT_ERRORS)
        self.assertIn(requests.exceptions.RequestException, _TRANSIENT_ERRORS)

    def test_every_market_writing_task_retries_only_transient_errors(self):
        for task in self.TASKS:
            with self.subTest(task=task.name):
                self.assertEqual(set(task.autoretry_for), set(_TRANSIENT_ERRORS))

    def test_every_market_writing_task_has_bounded_retries_and_backoff(self):
        for task in self.TASKS:
            with self.subTest(task=task.name):
                self.assertTrue(task.retry_backoff)
                self.assertIsNotNone(task.max_retries)
                self.assertGreater(task.max_retries, 0)
                self.assertLess(task.max_retries, 10)  # bounded, not "retry forever"

    def test_every_market_writing_task_has_a_time_limit(self):
        for task in self.TASKS:
            with self.subTest(task=task.name):
                self.assertIsNotNone(task.time_limit)
                self.assertIsNotNone(task.soft_time_limit)
                self.assertLess(task.soft_time_limit, task.time_limit)

    def test_a_bug_like_valueerror_is_not_in_the_retry_set(self):
        # A programming error must fail loudly, not retry-storm silently.
        for task in self.TASKS:
            with self.subTest(task=task.name):
                self.assertNotIn(ValueError, task.autoretry_for)
                self.assertNotIn(KeyError, task.autoretry_for)


class RetryMechanismDemonstrationTests(TestCase):
    """Proves the actual retry-then-succeed mechanism works end to end via
    Celery's synchronous .apply() (no broker needed), using the identical
    _TRANSIENT_ERRORS/autoretry_for/retry_backoff pattern the real tasks
    use — with backoff disabled so the test stays fast."""

    def test_transient_failure_is_retried_and_eventually_succeeds(self):
        attempts = {"count": 0}

        @shared_task(
            name="test.flaky_task",
            autoretry_for=_TRANSIENT_ERRORS,
            retry_backoff=False,
            max_retries=3,
        )
        def flaky():
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise TimeoutError("lock busy")
            return {"ok": True, "attempts": attempts["count"]}

        result = flaky.apply()
        self.assertTrue(result.successful())
        self.assertEqual(result.result["attempts"], 3)

    def test_non_transient_failure_is_not_retried(self):
        attempts = {"count": 0}

        @shared_task(
            name="test.buggy_task",
            autoretry_for=_TRANSIENT_ERRORS,
            retry_backoff=False,
            max_retries=3,
        )
        def buggy():
            attempts["count"] += 1
            raise ValueError("not a transient error")

        result = buggy.apply(throw=False)
        self.assertTrue(result.failed())
        self.assertEqual(attempts["count"], 1)  # no retry attempted

    def test_exhausting_retries_on_persistent_transient_failure_fails(self):
        @shared_task(
            name="test.always_busy_task",
            autoretry_for=_TRANSIENT_ERRORS,
            retry_backoff=False,
            max_retries=2,
        )
        def always_busy():
            raise TimeoutError("still busy")

        result = always_busy.apply(throw=False)
        self.assertTrue(result.failed())


class TaskStatusRecordingTests(TestCase):
    """@record_task_run must persist a TaskRun row independent of Celery's
    own result backend, for both success and failure."""

    def test_success_records_status_and_detail(self):
        @record_task_run("test.success_task")
        def ok():
            return {"processed": 5}

        result = ok()
        self.assertEqual(result, {"processed": 5})
        run = TaskRun.objects.get(task_name="test.success_task")
        self.assertEqual(run.status, TaskStatus.SUCCESS)
        self.assertEqual(run.detail, {"processed": 5})
        self.assertIsNotNone(run.finished_at)

    def test_failure_records_status_and_error_then_reraises(self):
        @record_task_run("test.failure_task")
        def boom():
            raise RuntimeError("kaboom")

        with self.assertRaises(RuntimeError):
            boom()
        run = TaskRun.objects.get(task_name="test.failure_task")
        self.assertEqual(run.status, TaskStatus.FAILURE)
        self.assertIn("kaboom", run.error)

    def test_each_call_creates_a_separate_run_row(self):
        @record_task_run("test.repeated_task")
        def noop():
            return {}

        noop()
        noop()
        self.assertEqual(TaskRun.objects.filter(task_name="test.repeated_task").count(), 2)

    def test_orphaned_started_run_is_closed_after_hard_limit_and_grace(self):
        run = TaskRun.objects.create(task_name="market.tasks.fetch_all_market_data", status=TaskStatus.STARTED)
        now = timezone.now()
        run.started_at = now - timedelta(seconds=600 + ORPHAN_GRACE_SECONDS + 1)
        run.save(update_fields=["started_at"])

        result = reconcile_orphaned_task_runs(now=now)

        self.assertEqual(result["run_ids"], [run.id])
        run.refresh_from_db()
        self.assertEqual(run.status, TaskStatus.FAILURE)
        self.assertIsNotNone(run.finished_at)
        self.assertIn("Orphaned task record", run.error)

    def test_recent_started_run_is_not_reconciled(self):
        run = TaskRun.objects.create(task_name="market.tasks.fetch_all_market_data", status=TaskStatus.STARTED)

        result = reconcile_orphaned_task_runs(now=timezone.now())

        self.assertEqual(result["reconciled"], 0)
        run.refresh_from_db()
        self.assertEqual(run.status, TaskStatus.STARTED)


class TaskLockingIntegrationTests(TestCase):
    """Market-writing tasks must actually go through exclusive_db_write —
    not just have the lock primitive exist somewhere unused."""

    @mock.patch("market.services.analyzer.run_full_analysis", return_value={"ok": True})
    def test_run_full_analysis_task_acquires_the_market_write_lock(self, mock_run):
        with mock.patch("market.services.autosync.exclusive_db_write") as mock_lock:
            mock_lock.return_value.__enter__ = mock.Mock(return_value=None)
            mock_lock.return_value.__exit__ = mock.Mock(return_value=False)
            run_full_analysis_task(train_ml=False)
        mock_lock.assert_called_once()
        mock_run.assert_called_once_with(train_ml=False)

    @mock.patch("market.services.ml_model.train_model", return_value={"ok": True})
    def test_train_ml_model_acquires_the_market_write_lock(self, mock_train):
        with mock.patch("market.services.autosync.exclusive_db_write") as mock_lock:
            mock_lock.return_value.__enter__ = mock.Mock(return_value=None)
            mock_lock.return_value.__exit__ = mock.Mock(return_value=False)
            train_ml_model()
        mock_lock.assert_called_once()
        mock_train.assert_called_once()

    def test_duplicate_task_invocation_is_rejected_while_lock_held(self):
        """A second worker attempting the same market-writing task while
        one is in progress must be rejected, not run concurrently."""
        from market.services.autosync import exclusive_db_write
        from market.services.locking import distributed_lock

        with distributed_lock("market-write", timeout=10, blocking_timeout=0):
            with self.assertRaises(TimeoutError):
                with exclusive_db_write(blocking=False):
                    pass  # pragma: no cover — a duplicate run must never reach here


class AppendDailyBarsIdempotencyTests(TestCase):
    """Re-running the daily append (retry, or a duplicate trigger) must
    upsert the same day's bar, not create duplicate PriceHistory rows."""

    def setUp(self):
        self.stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="IDEMP", company_name="Idemp Co")

    def _fake_live_sync(self, **kwargs):
        from django.utils import timezone

        today = timezone.localdate()
        PriceHistory.objects.update_or_create(
            stock=self.stock,
            date=today,
            defaults={"open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000},
        )
        return {"ok": True, "dse": {"ok": True, "count": 1}, "cse": {"ok": True, "count": 0}}

    def test_running_append_twice_does_not_duplicate_todays_bar(self):
        from django.utils import timezone

        with mock.patch(
            "market.services.autosync._run_live_sync_unlocked", side_effect=self._fake_live_sync
        ), mock.patch("market.services.analyzer.run_full_analysis", return_value={"analyzed": 0}):
            append_daily_bars()
            append_daily_bars()
        today = timezone.localdate()
        self.assertEqual(PriceHistory.objects.filter(stock=self.stock, date=today).count(), 1)


class SyncLiveMarketIdempotencyTests(TestCase):
    """Repeated ticks within the configured interval must be no-ops
    (idempotent freshness check), not repeated real fetches."""

    def setUp(self):
        # _state is process-global in autosync.py — reset it so this test
        # doesn't depend on what other tests happened to run before it.
        from market.services import autosync

        self._orig_state = dict(autosync._state)
        autosync._state.update(
            {"last_attempt": None, "last_success": None, "last_error": None, "last_result": None, "running": False, "source": None}
        )

    def tearDown(self):
        from market.services import autosync

        autosync._state.update(self._orig_state)

    def test_second_call_within_interval_skips_real_sync(self):
        from django.utils import timezone as tz

        from market.services import autosync

        def fake_run_live_sync(force=False):
            # Mirrors what the real run_live_sync/_run_live_sync_unlocked
            # does to _state on success, so maybe_sync's staleness check
            # (which reads _state) behaves realistically.
            autosync._state["last_success"] = tz.now()
            return {"ok": True, "dse": {"ok": True}, "cse": {"ok": True}}

        with mock.patch("market.services.autosync.run_live_sync", side_effect=fake_run_live_sync) as mock_run:
            sync_live_market()
            sync_live_market()
        # run_live_sync (the real network-touching path) is invoked only
        # once — the second call is recognized as still-fresh and skipped.
        self.assertEqual(mock_run.call_count, 1)
