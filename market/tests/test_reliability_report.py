"""
ML Reliability Monitor — orchestrator integration tests: model-version
isolation, exchange isolation, dry-run (no writes), settle-only, cross-
exchange divergence flagging, and immutable assessment history.
"""
from datetime import date, timedelta

from django.test import TestCase

from market.models import (
    Exchange,
    MLModelVersion,
    PredictionSnapshot,
    ReliabilityAssessment,
    Stock,
)
from market.services.ml_model import FEATURE_COLS
from market.services.reliability_capture import feature_schema_version
from market.services.reliability_metrics import MIN_SAMPLES_WATCH
from market.services.reliability_report import run_reliability_assessment


def _make_settled_snapshots(*, exchange, model_version_tag, family, n, horizon=10, skill_positive=True, start=date(2026, 1, 1), feature_schema=FEATURE_COLS):
    stock = Stock.objects.create(exchange=exchange, trading_code=f"RPT-{model_version_tag}-{exchange}", company_name="Report Co")
    schema_version = feature_schema_version(feature_schema)
    rows = []
    for i in range(n):
        d = start + timedelta(days=i)
        # skill_positive=True: predicted_class matches outcome_class every time (perfect skill).
        # skill_positive=False: predicted_class is the OPPOSITE of outcome every time (perfectly wrong -> negative skill).
        outcome = i % 2 == 0
        predicted = outcome if skill_positive else (not outcome)
        rows.append(
            PredictionSnapshot(
                model_family=family,
                model_version_tag=model_version_tag,
                feature_schema_version=schema_version,
                stock=stock,
                stock_trading_code=stock.trading_code,
                exchange=exchange,
                data_cutoff_date=d,
                horizon_trading_days=horizon,
                target_date=d + timedelta(days=horizon + 4),
                reference_close=100.0,
                predicted_class=predicted,
                predicted_probability=0.9 if predicted else 0.1,
                rule_baseline_class=True,
                naive_baseline_class=True,
                outcome_class=outcome,
                outcome_return=0.05 if outcome else -0.05,
                settlement_status=PredictionSnapshot.SettlementStatus.SETTLED,
            )
        )
    PredictionSnapshot.objects.bulk_create(rows)
    return stock


class ModelVersionIsolationTests(TestCase):
    """A retrain that keeps the same feature schema is a continuation of
    the same product, so its settled track record accumulates across
    versions — a model that retrains daily must not have its reliability
    window reset to zero every day (this was a real production bug: every
    window stuck at n=0 despite 1000+ real settled snapshots existing,
    because assessment used to isolate by exact version tag). A genuine
    feature-schema change is the one case that must still start a fresh,
    isolated window, since a differently-shaped model really is a
    different product whose track record shouldn't be blamed on/credited
    to a different one."""

    def test_same_schema_retrain_accumulates_across_versions(self):
        exchange = Exchange.DSE
        # Old version, same schema, perfectly WRONG — should still count:
        # retraining with an unchanged feature set is a continuation, not
        # a new product.
        _make_settled_snapshots(
            exchange=exchange, model_version_tag="old-v", family=PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF,
            n=MIN_SAMPLES_WATCH, skill_positive=False, start=date(2025, 1, 1),
        )
        MLModelVersion.objects.create(
            model_name="forward_return_rf", version="old-v", exchange_scope="combined", status="inactive",
            is_active=False, data_cutoff=date(2025, 6, 1), train_rows=100, feature_schema=FEATURE_COLS,
        )
        # New (active) version, same schema, perfectly RIGHT.
        _make_settled_snapshots(
            exchange=exchange, model_version_tag="new-v", family=PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF,
            n=MIN_SAMPLES_WATCH, skill_positive=True, start=date(2026, 1, 1),
        )
        MLModelVersion.objects.create(
            model_name="forward_return_rf", version="new-v", exchange_scope="combined", status="active",
            is_active=True, data_cutoff=date(2026, 6, 1), train_rows=100, feature_schema=FEATURE_COLS,
        )

        result = run_reliability_assessment(
            families=[PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF], exchanges=[exchange], windows=[MIN_SAMPLES_WATCH * 2],
        )
        assessment = result["assessments"][0]
        self.assertEqual(assessment["model_version_tag"], "new-v")
        # Both versions' snapshots pooled — this is the fix: same-schema
        # history is not thrown away on every retrain.
        self.assertEqual(assessment["sample_count"], MIN_SAMPLES_WATCH * 2)

    def test_feature_schema_change_starts_a_fresh_isolated_window(self):
        exchange = Exchange.DSE
        old_schema = FEATURE_COLS
        new_schema = FEATURE_COLS + ["extra_feature"]
        # Old version, OLD schema, perfectly WRONG — must NOT leak into the
        # new schema's window, since it's a genuinely different feature set.
        _make_settled_snapshots(
            exchange=exchange, model_version_tag="old-v", family=PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF,
            n=MIN_SAMPLES_WATCH, skill_positive=False, start=date(2025, 1, 1), feature_schema=old_schema,
        )
        MLModelVersion.objects.create(
            model_name="forward_return_rf", version="old-v", exchange_scope="combined", status="inactive",
            is_active=False, data_cutoff=date(2025, 6, 1), train_rows=100, feature_schema=old_schema,
        )
        # New (active) version, NEW schema, perfectly RIGHT.
        _make_settled_snapshots(
            exchange=exchange, model_version_tag="new-v", family=PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF,
            n=MIN_SAMPLES_WATCH, skill_positive=True, start=date(2026, 1, 1), feature_schema=new_schema,
        )
        MLModelVersion.objects.create(
            model_name="forward_return_rf", version="new-v", exchange_scope="combined", status="active",
            is_active=True, data_cutoff=date(2026, 6, 1), train_rows=100, feature_schema=new_schema,
        )

        result = run_reliability_assessment(
            families=[PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF], exchanges=[exchange], windows=[MIN_SAMPLES_WATCH],
        )
        assessment = result["assessments"][0]
        self.assertEqual(assessment["model_version_tag"], "new-v")
        # Only the new schema's own snapshots counted — the old schema's
        # bad record does not leak in (if it had, this would be 2x and
        # accuracy below would be dragged down to ~0.5).
        self.assertEqual(assessment["sample_count"], MIN_SAMPLES_WATCH)
        self.assertEqual(assessment["metrics"]["classification"]["model"]["accuracy"], 1.0)

    def test_explicit_version_flag_evaluates_that_specific_version(self):
        exchange = Exchange.DSE
        _make_settled_snapshots(
            exchange=exchange, model_version_tag="old-v", family=PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF,
            n=MIN_SAMPLES_WATCH, skill_positive=False, start=date(2025, 1, 1),
        )
        MLModelVersion.objects.create(
            model_name="forward_return_rf", version="old-v", exchange_scope="combined", status="inactive",
            is_active=False, data_cutoff=date(2025, 6, 1), train_rows=100, feature_schema=FEATURE_COLS,
        )
        result = run_reliability_assessment(
            families=[PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF],
            exchanges=[exchange],
            windows=[MIN_SAMPLES_WATCH],
            version_tag="old-v",
        )
        assessment = result["assessments"][0]
        self.assertEqual(assessment["model_version_tag"], "old-v")
        self.assertEqual(assessment["sample_count"], MIN_SAMPLES_WATCH)
        self.assertEqual(assessment["status"], ReliabilityAssessment.Status.CRITICAL)


class ExchangeIsolationTests(TestCase):
    def test_dse_snapshots_do_not_leak_into_cse_assessment(self):
        MLModelVersion.objects.create(
            model_name="forward_return_rf", version="v1", exchange_scope="combined", status="active",
            is_active=True, data_cutoff=date(2026, 6, 1), train_rows=100, feature_schema=FEATURE_COLS,
        )
        _make_settled_snapshots(
            exchange=Exchange.DSE, model_version_tag="v1", family=PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF,
            n=MIN_SAMPLES_WATCH, skill_positive=True,
        )
        result = run_reliability_assessment(
            families=[PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF], windows=[MIN_SAMPLES_WATCH],
        )
        by_exchange = {a["exchange"]: a for a in result["assessments"]}
        self.assertEqual(by_exchange[Exchange.DSE]["sample_count"], MIN_SAMPLES_WATCH)
        self.assertEqual(by_exchange[Exchange.CSE]["sample_count"], 0)
        self.assertEqual(by_exchange[Exchange.CSE]["status"], ReliabilityAssessment.Status.INSUFFICIENT_DATA)


class CrossExchangeDivergenceTests(TestCase):
    def test_diverging_dse_cse_skill_produces_a_cross_exchange_flag(self):
        MLModelVersion.objects.create(
            model_name="forward_return_rf", version="v1", exchange_scope="combined", status="active",
            is_active=True, data_cutoff=date(2026, 6, 1), train_rows=100, feature_schema=FEATURE_COLS,
        )
        _make_settled_snapshots(
            exchange=Exchange.DSE, model_version_tag="v1", family=PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF,
            n=MIN_SAMPLES_WATCH, skill_positive=True,
        )
        _make_settled_snapshots(
            exchange=Exchange.CSE, model_version_tag="v1", family=PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF,
            n=MIN_SAMPLES_WATCH, skill_positive=False,
        )
        result = run_reliability_assessment(
            families=[PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF], windows=[MIN_SAMPLES_WATCH],
        )
        self.assertTrue(result["cross_exchange_flags"])
        flag = result["cross_exchange_flags"][0]
        self.assertEqual(flag["action"], "separate_dse_cse_models")
        self.assertIn("DSE", flag["reason"])
        self.assertIn("CSE", flag["reason"])


class DryRunAndSettleOnlyTests(TestCase):
    def test_dry_run_persists_nothing(self):
        before_snapshots = PredictionSnapshot.objects.count()
        before_assessments = ReliabilityAssessment.objects.count()
        result = run_reliability_assessment(dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(PredictionSnapshot.objects.count(), before_snapshots)
        self.assertEqual(ReliabilityAssessment.objects.count(), before_assessments)
        # Dry-run still returns a preview of what the assessment would say.
        self.assertIn("assessments", result)

    def test_settle_only_skips_assessment_entirely(self):
        result = run_reliability_assessment(settle_only=True)
        self.assertNotIn("assessments", result)
        self.assertIn("settlement", result)


class AssessmentHistoryImmutabilityTests(TestCase):
    def test_repeated_runs_accumulate_history_rather_than_overwrite(self):
        MLModelVersion.objects.create(
            model_name="forward_return_rf", version="v1", exchange_scope="combined", status="active",
            is_active=True, data_cutoff=date(2026, 6, 1), train_rows=100, feature_schema=FEATURE_COLS,
        )
        _make_settled_snapshots(
            exchange=Exchange.DSE, model_version_tag="v1", family=PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF,
            n=MIN_SAMPLES_WATCH, skill_positive=True,
        )
        run_reliability_assessment(families=[PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF], windows=[MIN_SAMPLES_WATCH])
        first_count = ReliabilityAssessment.objects.count()
        self.assertGreater(first_count, 0)

        run_reliability_assessment(families=[PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF], windows=[MIN_SAMPLES_WATCH])
        second_count = ReliabilityAssessment.objects.count()
        self.assertEqual(second_count, first_count * 2)  # new rows, nothing overwritten
