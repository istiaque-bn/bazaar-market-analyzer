"""Telegram ML daily report — deterministic content tests. Pure
function/DB-fixture tests only; no Celery, no Telegram network calls
(see notifications/tests.py for scheduling/delivery/authorization
tests that exercise the actual send task)."""
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from market.models import Exchange, MLModelVersion, PredictionSnapshot, ReliabilityAssessment
from market.services.ml_daily_report import (
    DISCLAIMER,
    build_report_context,
    evidence_label,
    generate_recommendations,
    render_report_sections,
    split_for_telegram,
)

FAMILY = PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF
HORIZON = 10


@override_settings(ENABLE_DSE=True, ENABLE_CSE=True)
class MLDailyReportTestCase(TestCase):
    """Base for this file's tests: fixtures below always train a
    "combined"-scope MLModelVersion, and _resolve_scope only considers
    "combined" when every exchange is enabled (matching the actual
    serving path) — hardcode both exchanges on, same rationale as
    config/settings/test.py, so these tests don't flip meaning depending
    on the machine's local .env (e.g. a DSE-only deployment's ENABLE_CSE=False)."""


def _classifier_metrics(n, precision, accuracy, positive_rate_pred, calibration_error=0.05):
    return {
        "n": n,
        "precision": precision,
        "accuracy": accuracy,
        "recall": 0.5,
        "balanced_accuracy": accuracy,
        "direction_hit_rate": accuracy,
        "positive_rate_true": 0.5,
        "positive_rate_pred": positive_rate_pred,
        "calibration_error": calibration_error,
        "brier": 0.2,
    }


def make_model_version(*, is_active=True, status="active", hist_n=150, hist_precision=0.62, hist_pos_rate=0.5, trained_days_ago=3, version="v1"):
    obj = MLModelVersion.objects.create(
        model_name="forward_return_rf",
        version=version,
        exchange_scope="combined",
        status=status,
        is_active=is_active,
        data_cutoff=timezone.localdate(),
        train_rows=1000,
        metrics={"model": _classifier_metrics(hist_n, hist_precision, 0.6, hist_pos_rate)},
    )
    # trained_at is auto_now_add=True, so .create(trained_at=...) is
    # silently ignored — backdate it via a bare update() (bypasses
    # auto_now_add, which only fires on save()) so trained_days_ago tests
    # are meaningful.
    if trained_days_ago:
        MLModelVersion.objects.filter(pk=obj.pk).update(trained_at=timezone.now() - timedelta(days=trained_days_ago))
        obj.refresh_from_db()
    return obj


def make_assessment(*, model_version, exchange=Exchange.DSE, status=ReliabilityAssessment.Status.HEALTHY, sample_count=150, precision=0.6, accuracy=0.58, positive_rate_pred=0.5, calibration_error=0.05, window_label="365"):
    return ReliabilityAssessment.objects.create(
        model_family=FAMILY,
        model_version=model_version,
        model_version_tag=model_version.version if model_version else "",
        exchange=exchange,
        horizon_trading_days=HORIZON,
        window_label=window_label,
        window_size=365,
        sample_count=sample_count,
        status=status,
        reasons=["test fixture"],
        recommendations=[],
        metrics={"classification": {"n": sample_count, "model": _classifier_metrics(sample_count, precision, accuracy, positive_rate_pred, calibration_error)}, "economic": {}},
    )


class EvidenceLabelTests(MLDailyReportTestCase):
    def test_thresholds(self):
        self.assertEqual(evidence_label(0), "No evidence")
        self.assertEqual(evidence_label(29), "Very limited")
        self.assertEqual(evidence_label(30), "Limited")
        self.assertEqual(evidence_label(99), "Limited")
        self.assertEqual(evidence_label(100), "Moderate")
        self.assertEqual(evidence_label(299), "Moderate")
        self.assertEqual(evidence_label(300), "Stronger evidence")
        self.assertEqual(evidence_label(10_000), "Stronger evidence")


class NoActiveModelTests(MLDailyReportTestCase):
    def test_no_model_ever_trained(self):
        ctx = build_report_context()
        self.assertIsNone(ctx["active_model"])
        self.assertEqual(ctx["status_label"], "No evidence")
        self.assertIn("rule-based analysis", ctx["status_sentence"])

    def test_candidate_trained_but_never_qualified(self):
        make_model_version(is_active=False, status="experimental")
        ctx = build_report_context()
        self.assertIsNone(ctx["active_model"])
        self.assertEqual(ctx["status_label"], "Suspended")

    def test_never_displays_zero_percent_for_no_observations(self):
        ctx = build_report_context()
        sections = render_report_sections(ctx)
        joined = "\n".join(sections)
        self.assertNotIn("0%", joined)
        self.assertNotIn("correct about 0 times out of 100", joined)


class ActiveModelLimitedEvidenceTests(MLDailyReportTestCase):
    def setUp(self):
        self.model = make_model_version(hist_n=148, hist_precision=0.62, hist_pos_rate=0.5)

    def test_no_settled_predictions_yet_is_experimental(self):
        ctx = build_report_context()
        self.assertEqual(ctx["status_label"], "Experimental")
        self.assertEqual(ctx["live"]["n"], 0)
        self.assertEqual(ctx["evidence"], "No evidence")

    def test_render_says_not_enough_live_evidence(self):
        ctx = build_report_context()
        sections = render_report_sections(ctx)
        joined = "\n".join(sections)
        self.assertIn("not enough completed live evidence", joined)

    def test_historical_precision_uses_out_of_sample_metrics_not_zero(self):
        ctx = build_report_context()
        self.assertEqual(ctx["historical"]["n"], 148)
        self.assertEqual(ctx["historical"]["precision"], 0.62)
        sections = render_report_sections(ctx)
        joined = "\n".join(sections)
        self.assertIn("62 times out of 100", joined)
        self.assertIn("148 historical test predictions", joined)

    def test_few_settled_predictions_recommends_collecting_evidence(self):
        make_assessment(model_version=self.model, status=ReliabilityAssessment.Status.INSUFFICIENT_DATA, sample_count=10)
        ctx = build_report_context()
        self.assertEqual(ctx["status_label"], "Promising")
        self.assertIn("Collect more completed live predictions before changing the model.", ctx["recommendations"])


class ActiveModelSufficientEvidenceTests(MLDailyReportTestCase):
    def setUp(self):
        self.model = make_model_version(hist_n=200, hist_precision=0.6, hist_pos_rate=0.5)
        self.assessment = make_assessment(
            model_version=self.model, status=ReliabilityAssessment.Status.HEALTHY, sample_count=150, precision=0.58, accuracy=0.58, positive_rate_pred=0.55
        )

    def test_status_stable_with_moderate_evidence(self):
        ctx = build_report_context()
        self.assertEqual(ctx["evidence"], "Moderate")
        self.assertEqual(ctx["status_label"], "Stable")

    def test_live_and_historical_never_mixed(self):
        ctx = build_report_context()
        self.assertNotEqual(ctx["historical"]["precision"], ctx["live"]["precision"])
        sections = render_report_sections(ctx)
        joined = "\n".join(sections)
        self.assertIn("Historical test", joined)
        self.assertIn("Live so far", joined)

    def test_directional_result_not_labeled_as_precision(self):
        ctx = build_report_context()
        sections = render_report_sections(ctx)
        joined = "\n".join(sections)
        self.assertIn("The predicted direction was correct in", joined)
        self.assertNotIn("precision was correct in", joined)

    def test_sample_counts_appear_beside_percentages(self):
        ctx = build_report_context()
        sections = render_report_sections(ctx)
        joined = "\n".join(sections)
        self.assertIn("200 historical test predictions", joined)
        self.assertIn("150 completed live predictions", joined)

    def test_disclaimer_present(self):
        ctx = build_report_context()
        sections = render_report_sections(ctx)
        self.assertEqual(sections[-1], DISCLAIMER)


class DecliningPerformanceTests(MLDailyReportTestCase):
    def setUp(self):
        self.model = make_model_version(hist_n=200, hist_precision=0.55, hist_pos_rate=0.5)
        make_assessment(model_version=self.model, status=ReliabilityAssessment.Status.CRITICAL, sample_count=150, precision=0.4, accuracy=0.4, positive_rate_pred=0.5)

    def test_status_declining(self):
        ctx = build_report_context()
        self.assertEqual(ctx["status_label"], "Declining")
        self.assertIn("weakened", ctx["status_sentence"])

    def test_recommends_pausing_promotion(self):
        ctx = build_report_context()
        self.assertIn("Pause automatic promotion and investigate recent predictions.", ctx["recommendations"])


class CandidateFailedGateTests(MLDailyReportTestCase):
    def test_newer_failed_candidate_flagged_and_recommended(self):
        active = make_model_version(is_active=True, status="active", trained_days_ago=10, version="v1")
        make_model_version(is_active=False, status="experimental", trained_days_ago=1, version="v2")
        ctx = build_report_context()
        self.assertTrue(ctx["candidate_failed"])
        self.assertIn("did not beat the simple comparison", ctx["status_sentence"])
        self.assertIn("Keep the existing approved model and investigate the failed candidate.", ctx["recommendations"])
        self.assertEqual(ctx["active_model"].version, active.version)


class WeakCalibrationTests(MLDailyReportTestCase):
    def test_high_calibration_error_recommends_confidence_adjustment(self):
        model = make_model_version(hist_n=150)
        make_assessment(model_version=model, status=ReliabilityAssessment.Status.WATCH, sample_count=150, calibration_error=0.3)
        ctx = build_report_context()
        self.assertIn(
            "The confidence percentages appear too optimistic or too cautious and need adjustment.",
            ctx["recommendations"],
        )


class RecommendationCountTests(MLDailyReportTestCase):
    def test_maximum_three_recommendations(self):
        recs = generate_recommendations(
            assessment=type("A", (), {"status": ReliabilityAssessment.Status.CRITICAL})(),
            candidate_failed=True,
            calibration_error=0.5,
            live_n=1,
            stale_model=True,
            data_quality_flags=["stale"],
        )
        self.assertLessEqual(len(recs), 3)

    def test_no_concerns_gives_neutral_fallback(self):
        recs = generate_recommendations(
            assessment=None, candidate_failed=False, calibration_error=None, live_n=1000, stale_model=False, data_quality_flags=[]
        )
        self.assertEqual(recs, ["No specific concerns detected — continue routine monitoring."])


class PlainLanguageTests(MLDailyReportTestCase):
    """No unexplained technical jargon leaks into the Telegram text."""

    BANNED_TERMS = [
        "brier",
        "embargo",
        "walk-forward fold",
        "class imbalance",
        "calibration error",
        "feature schema",
        "hyperparameter",
        "distribution drift",
        "p-value",
        "confusion matrix",
        "RandomForestClassifier",
        "RandomForestRegressor",
        "XGBClassifier",
    ]

    def test_no_banned_jargon_in_rendered_report(self):
        make_model_version(hist_n=150)
        make_assessment(model_version=MLModelVersion.objects.first(), status=ReliabilityAssessment.Status.WATCH, sample_count=150, calibration_error=0.3)
        ctx = build_report_context()
        joined = "\n".join(render_report_sections(ctx)).lower()
        for term in self.BANNED_TERMS:
            self.assertNotIn(term.lower(), joined)


class SplitForTelegramTests(MLDailyReportTestCase):
    def test_never_splits_mid_section(self):
        sections = ["a" * 100, "b" * 100, "c" * 100]
        chunks = split_for_telegram(sections, limit=150)
        rejoined = "".join(chunks).replace("\n\n", "")
        self.assertEqual(rejoined, "a" * 100 + "b" * 100 + "c" * 100)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 250)  # generous — two sections can share a chunk

    def test_preserves_order(self):
        sections = ["first", "second", "third"]
        chunks = split_for_telegram(sections, limit=5)
        self.assertEqual(chunks, ["first", "second", "third"])

    def test_single_chunk_when_under_limit(self):
        sections = ["short one", "short two"]
        chunks = split_for_telegram(sections, limit=1000)
        self.assertEqual(len(chunks), 1)


class TrainedTodayTests(MLDailyReportTestCase):
    def test_trained_today_true_when_trained_today(self):
        make_model_version(trained_days_ago=0)
        ctx = build_report_context()
        self.assertTrue(ctx["trained_today"])

    def test_trained_today_false_when_trained_earlier(self):
        make_model_version(trained_days_ago=3)
        ctx = build_report_context()
        self.assertFalse(ctx["trained_today"])
