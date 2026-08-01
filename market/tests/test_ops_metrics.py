"""Phase 9 — market.services.ops_metrics."""
from datetime import date, timedelta

from django.test import TestCase
from django.utils import timezone

from market.models import AnalysisResult, Exchange, SignalAction, Stock, TaskRun, TaskStatus
from market.services.ops_metrics import (
    model_evaluation_summary,
    ops_summary,
    prediction_volume,
    rejected_rows_summary,
    task_health,
)


def _mk_task_run(task_name, status, *, started_at, finished_at=None, error=""):
    # started_at has auto_now_add=True — it's forced to "now" on INSERT
    # regardless of what's passed to create(), so backdating it needs a
    # separate UPDATE after the row exists (auto_now_add only fires on
    # the initial insert, not on later .save() calls).
    run = TaskRun.objects.create(task_name=task_name, status=status, finished_at=finished_at, error=error)
    run.started_at = started_at
    run.save(update_fields=["started_at"])
    return run


class TaskHealthTests(TestCase):
    def test_counts_runs_failures_and_average_duration_per_task(self):
        started = timezone.now() - timedelta(minutes=10)
        _mk_task_run(
            "market.tasks.sync_live_market", TaskStatus.SUCCESS, started_at=started, finished_at=started + timedelta(seconds=4)
        )
        _mk_task_run(
            "market.tasks.sync_live_market",
            TaskStatus.FAILURE,
            started_at=started + timedelta(minutes=1),
            finished_at=started + timedelta(minutes=1, seconds=6),
            error="boom",
        )
        rows = task_health()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["task_name"], "market.tasks.sync_live_market")
        self.assertEqual(row["runs"], 2)
        self.assertEqual(row["failures"], 1)
        self.assertAlmostEqual(row["avg_duration_seconds"], 5.0, places=1)
        self.assertEqual(row["latest_status"], TaskStatus.FAILURE)
        self.assertIn("boom", row["latest_error"])

    def test_outside_window_runs_are_excluded(self):
        _mk_task_run("market.tasks.train_ml_model", TaskStatus.SUCCESS, started_at=timezone.now() - timedelta(days=30))
        self.assertEqual(task_health(window_days=7), [])


class PredictionVolumeTests(TestCase):
    def _mk_stock(self, code):
        return Stock.objects.create(exchange=Exchange.DSE, trading_code=code, is_active=True)

    def test_reports_latest_session_count_and_trend(self):
        today = timezone.localdate()
        yesterday = today - timedelta(days=1)
        for i, as_of in enumerate([yesterday, yesterday, today]):
            AnalysisResult.objects.create(
                stock=self._mk_stock(f"S{i}"), as_of=as_of, action=SignalAction.HOLD, score=0
            )
        result = prediction_volume()
        self.assertEqual(result["latest_as_of"], today.isoformat())
        self.assertEqual(result["latest_count"], 1)
        trend_by_date = {row["as_of"]: row["count"] for row in result["trend"]}
        self.assertEqual(trend_by_date[yesterday.isoformat()], 2)
        self.assertEqual(trend_by_date[today.isoformat()], 1)

    def test_no_analysis_rows_yields_empty_but_valid_shape(self):
        result = prediction_volume()
        self.assertIsNone(result["latest_as_of"])
        self.assertEqual(result["latest_count"], 0)
        self.assertEqual(result["trend"], [])


class RejectedRowsSummaryTests(TestCase):
    def test_delegates_to_provenance_report_and_returns_expected_keys(self):
        result = rejected_rows_summary()
        self.assertIn("flagged_rows", result)
        self.assertIn("flag_counts", result)
        self.assertIn("freshness", result)
        self.assertIn(Exchange.DSE, result["freshness"])
        self.assertIn(Exchange.CSE, result["freshness"])


class ModelEvaluationSummaryTests(TestCase):
    def test_reports_both_learned_layers_with_no_models_trained_yet(self):
        result = model_evaluation_summary()
        self.assertIn(Exchange.DSE, result["forward_return_model"])
        self.assertIn(Exchange.CSE, result["forward_return_model"])
        self.assertFalse(result["forward_return_model"][Exchange.DSE]["deployed"])
        self.assertIn("skill_vs_naive", result["next_close_model"])


class OpsSummaryTests(TestCase):
    def test_gathers_everything_in_one_call(self):
        summary = ops_summary()
        for key in ("generated_at", "tasks", "predictions", "rejected_rows", "models"):
            self.assertIn(key, summary)
