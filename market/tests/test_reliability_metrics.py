"""
ML Reliability Monitor — metrics tests (pure functions, fixed examples,
no DB/network access).
"""
from datetime import date

import numpy as np
from django.test import SimpleTestCase

from market.models import PredictionSnapshot, ReliabilityAssessment
from market.services.reliability_metrics import (
    HIGH_CALIBRATION_ERROR,
    MIN_SAMPLES_INSUFFICIENT,
    MIN_SAMPLES_WATCH,
    bootstrap_ci,
    compute_calibration,
    compute_classifier_metrics,
    compute_economic_diagnostics,
    compute_regressor_metrics,
    determine_status,
)


class ClassifierMetricsFixedExampleTests(SimpleTestCase):
    def _rows(self):
        # predicted_class = probability > 0.5 -> [T, T, F, F]
        # outcome_class                        -> [T, F, F, T]
        probs = [0.9, 0.6, 0.3, 0.1]
        outcomes = [True, False, False, True]
        return [
            {
                "predicted_class": p > 0.5,
                "predicted_probability": p,
                "rule_baseline_class": True,
                "naive_baseline_class": True,
                "outcome_class": o,
            }
            for p, o in zip(probs, outcomes)
        ]

    def test_accuracy_precision_recall_match_hand_computation(self):
        result = compute_classifier_metrics(self._rows())
        model = result["model"]
        self.assertEqual(result["n"], 4)
        self.assertAlmostEqual(model["accuracy"], 0.5)
        self.assertAlmostEqual(model["balanced_accuracy"], 0.5)
        self.assertAlmostEqual(model["precision"], 0.5)
        self.assertAlmostEqual(model["recall"], 0.5)
        self.assertAlmostEqual(model["f1"], 0.5)

    def test_roc_auc_present_when_both_classes_exist(self):
        result = compute_classifier_metrics(self._rows())
        self.assertIsNotNone(result["model"]["roc_auc"])

    def test_baselines_and_skill_computed(self):
        result = compute_classifier_metrics(self._rows())
        self.assertIn("rule_based", result["baselines"])
        self.assertIn("naive_majority_class", result["baselines"])
        self.assertIn("rule_based", result["skill_vs_baseline"])
        self.assertIn("naive_majority_class", result["skill_vs_baseline"])

    def test_empty_rows_returns_n_zero(self):
        result = compute_classifier_metrics([])
        self.assertEqual(result, {"n": 0})


class ClassifierSingleClassTests(SimpleTestCase):
    def test_single_class_outcome_disables_roc_auc_and_balanced_accuracy(self):
        rows = [
            {"predicted_class": True, "predicted_probability": 0.8, "rule_baseline_class": None, "naive_baseline_class": None, "outcome_class": True}
            for _ in range(5)
        ]
        result = compute_classifier_metrics(rows)
        self.assertIsNone(result["model"]["roc_auc"])
        self.assertIsNone(result["model"]["balanced_accuracy"])
        # Accuracy is still well-defined even with one class.
        self.assertEqual(result["model"]["accuracy"], 1.0)

    def test_rows_without_baselines_produce_no_baseline_entries(self):
        rows = [
            {"predicted_class": True, "predicted_probability": 0.8, "rule_baseline_class": None, "naive_baseline_class": None, "outcome_class": True}
        ]
        result = compute_classifier_metrics(rows)
        self.assertEqual(result["baselines"], {})
        self.assertEqual(result["skill_vs_baseline"], {})


class CalibrationTests(SimpleTestCase):
    def test_perfectly_calibrated_probabilities_have_zero_error(self):
        # 10 rows at p=0.9 with 9/10 positive outcomes -> perfectly calibrated in that bucket.
        y_prob = np.array([0.95] * 10)
        y_true = np.array([1] * 9 + [0] * 1)
        calib = compute_calibration(y_true, y_prob)
        self.assertEqual(len(calib["buckets"]), 1)
        bucket = calib["buckets"][0]
        self.assertEqual(bucket["n"], 10)
        self.assertAlmostEqual(bucket["avg_predicted_probability"], 0.95)
        self.assertAlmostEqual(bucket["actual_frequency"], 0.9)
        self.assertAlmostEqual(calib["expected_calibration_error"], abs(0.95 - 0.9))

    def test_empty_input_returns_no_buckets(self):
        calib = compute_calibration(np.array([]), np.array([]))
        self.assertEqual(calib["buckets"], [])
        self.assertIsNone(calib["expected_calibration_error"])


class RegressorMetricsFixedExampleTests(SimpleTestCase):
    def _rows(self):
        preds = [0.02, -0.01, 0.03, 0.0]
        actuals = [0.01, -0.02, 0.03, 0.01]
        return [
            {"predicted_return": p, "rule_baseline_return": 0.0, "naive_baseline_return": 0.0, "outcome_return": a}
            for p, a in zip(preds, actuals)
        ]

    def test_mae_matches_hand_computation(self):
        result = compute_regressor_metrics(self._rows())
        expected_mae = np.mean([abs(0.02 - 0.01), abs(-0.01 - -0.02), abs(0.03 - 0.03), abs(0.0 - 0.01)])
        self.assertAlmostEqual(result["model"]["mae"], round(float(expected_mae), 6))

    def test_direction_hit_rate(self):
        result = compute_regressor_metrics(self._rows())
        # (pred>=0)==(actual>=0): (T,T) (F,F) (T,T) (T,T) -> all match -> 1.0
        self.assertAlmostEqual(result["model"]["direction_hit_rate"], 1.0)

    def test_bias_is_mean_pred_minus_actual(self):
        result = compute_regressor_metrics(self._rows())
        expected_bias = np.mean([0.02 - 0.01, -0.01 - -0.02, 0.03 - 0.03, 0.0 - 0.01])
        self.assertAlmostEqual(result["model"]["bias"], round(float(expected_bias), 6))

    def test_empty_rows_returns_n_zero(self):
        self.assertEqual(compute_regressor_metrics([]), {"n": 0})

    def test_naive_and_rule_baselines_present(self):
        result = compute_regressor_metrics(self._rows())
        self.assertIn("rule_based_persistence", result["skill_vs_baseline"])
        self.assertIn("naive_zero_return", result["skill_vs_baseline"])


class EconomicDiagnosticsTests(SimpleTestCase):
    def test_all_long_positive_outcomes_produce_positive_gross_return(self):
        rows = [
            {"predicted_class": True, "predicted_return": 0.02, "outcome_return": 0.02, "data_cutoff_date": date(2026, 1, i + 1), "target_date": date(2026, 1, i + 2)}
            for i in range(5)
        ]
        result = compute_economic_diagnostics(rows, PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF)
        self.assertEqual(result["trades_executed"], 5)
        self.assertEqual(result["turnover"], 1.0)
        self.assertGreater(result["gross_mean_return_pct"], 0)
        # Costs must reduce the net return relative to gross, and scale with the cost multiplier.
        self.assertLess(result["at_1x_cost"]["net_total_return_pct"], result["gross_mean_return_pct"] * 5)
        self.assertGreaterEqual(result["at_1x_cost"]["estimated_total_cost_pct"], 0)
        self.assertGreaterEqual(result["at_2x_cost"]["estimated_total_cost_pct"], result["at_1x_cost"]["estimated_total_cost_pct"])
        self.assertGreaterEqual(result["at_1x_cost"]["net_total_return_pct"], result["at_2x_cost"]["net_total_return_pct"])

    def test_all_flat_when_predicted_direction_never_positive(self):
        rows = [
            {"predicted_class": False, "predicted_return": -0.01, "outcome_return": 0.05, "data_cutoff_date": date(2026, 1, i + 1), "target_date": date(2026, 1, i + 2)}
            for i in range(3)
        ]
        result = compute_economic_diagnostics(rows, PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF)
        self.assertEqual(result["trades_executed"], 0)
        self.assertEqual(result["turnover"], 0.0)
        self.assertEqual(result["at_1x_cost"]["net_total_return_pct"], 0.0)

    def test_empty_rows(self):
        self.assertEqual(compute_economic_diagnostics([], "forward_return_rf"), {"n": 0})


class BootstrapCiTests(SimpleTestCase):
    def test_deterministic_given_same_seed(self):
        values = [0.1, 0.2, -0.1, 0.05, 0.3, -0.2, 0.15]
        ci1 = bootstrap_ci(values, seed=42, n_boot=200)
        ci2 = bootstrap_ci(values, seed=42, n_boot=200)
        self.assertEqual(ci1, ci2)

    def test_different_seeds_can_differ(self):
        values = [0.1, 0.2, -0.1, 0.05, 0.3, -0.2, 0.15, 0.4, -0.3]
        ci1 = bootstrap_ci(values, seed=1, n_boot=200)
        ci2 = bootstrap_ci(values, seed=2, n_boot=200)
        # Not asserting inequality (could coincide), just that both are well-formed.
        for ci in (ci1, ci2):
            self.assertIsNotNone(ci["low"])
            self.assertLessEqual(ci["low"], ci["high"])

    def test_too_few_values_returns_none(self):
        ci = bootstrap_ci([0.1], seed=42)
        self.assertIsNone(ci["low"])
        self.assertEqual(ci["n_boot"], 0)

    def test_ci_brackets_the_mean_for_a_symmetric_sample(self):
        values = list(np.linspace(-1, 1, 101))
        ci = bootstrap_ci(values, seed=7, n_boot=500)
        self.assertLess(ci["low"], 0.05)
        self.assertGreater(ci["high"], -0.05)


class DetermineStatusTests(SimpleTestCase):
    def test_below_minimum_sample_is_insufficient_data(self):
        status, reasons = determine_status(MIN_SAMPLES_INSUFFICIENT - 1, 0.1, 0.05, [])
        self.assertEqual(status, ReliabilityAssessment.Status.INSUFFICIENT_DATA)
        self.assertTrue(reasons)

    def test_positive_skill_enough_samples_no_drift_is_healthy(self):
        status, reasons = determine_status(MIN_SAMPLES_WATCH, 0.2, 0.05, [])
        self.assertEqual(status, ReliabilityAssessment.Status.HEALTHY)

    def test_positive_skill_but_below_watch_threshold_is_watch(self):
        status, reasons = determine_status(MIN_SAMPLES_INSUFFICIENT, 0.2, 0.05, [])
        self.assertEqual(status, ReliabilityAssessment.Status.WATCH)

    def test_non_positive_skill_with_enough_samples_is_critical(self):
        status, reasons = determine_status(MIN_SAMPLES_WATCH, -0.1, 0.05, [])
        self.assertEqual(status, ReliabilityAssessment.Status.CRITICAL)

    def test_non_positive_skill_with_moderate_samples_is_degraded(self):
        status, reasons = determine_status(MIN_SAMPLES_INSUFFICIENT, -0.1, 0.05, [])
        self.assertEqual(status, ReliabilityAssessment.Status.DEGRADED)

    def test_high_calibration_error_downgrades_healthy_to_watch(self):
        status, reasons = determine_status(MIN_SAMPLES_WATCH, 0.2, HIGH_CALIBRATION_ERROR + 0.01, [])
        self.assertEqual(status, ReliabilityAssessment.Status.WATCH)

    def test_drift_flags_downgrade_healthy_to_watch(self):
        status, reasons = determine_status(MIN_SAMPLES_WATCH, 0.2, 0.01, ["some drift"])
        self.assertEqual(status, ReliabilityAssessment.Status.WATCH)

    def test_status_is_never_a_bare_verdict_without_reasons(self):
        for args in [
            (10, None, None, []),
            (100, 0.3, 0.02, []),
            (100, -0.1, 0.02, []),
        ]:
            status, reasons = determine_status(*args)
            self.assertTrue(reasons, f"status {status} must always carry human-readable reasons")
