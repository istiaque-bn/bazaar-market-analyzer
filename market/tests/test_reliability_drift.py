"""
ML Reliability Monitor — drift detection tests: PSI/KS sanity (drift vs.
no-drift controls), small-sample safety, and feature-schema-change
detection.
"""
from datetime import date, timedelta

import numpy as np
from django.test import SimpleTestCase

from market.models import MLModelVersion, PredictionSnapshot
from market.services.ml_model import FEATURE_COLS
from market.services.reliability_drift import (
    MIN_ROWS_PER_HALF,
    assess_drift,
    assess_feature_schema,
    ks_test,
    population_stability_index,
)


class PsiNoDriftControlTests(SimpleTestCase):
    def test_identical_distributions_have_near_zero_psi(self):
        rng = np.random.default_rng(0)
        ref = rng.normal(0, 1, 500)
        cur = rng.normal(0, 1, 500)
        psi = population_stability_index(ref, cur)
        self.assertIsNotNone(psi)
        self.assertLess(psi, 0.1)

    def test_shifted_distribution_has_high_psi(self):
        rng = np.random.default_rng(0)
        ref = rng.normal(0, 1, 500)
        cur = rng.normal(5, 1, 500)  # materially shifted mean
        psi = population_stability_index(ref, cur)
        self.assertGreater(psi, 0.25)

    def test_too_few_rows_returns_none(self):
        self.assertIsNone(population_stability_index([1, 2, 3], [1, 2, 3]))


class KsTestTests(SimpleTestCase):
    def test_identical_distributions_have_high_p_value(self):
        rng = np.random.default_rng(1)
        ref = rng.normal(0, 1, 300)
        cur = rng.normal(0, 1, 300)
        result = ks_test(ref, cur)
        self.assertGreater(result["p_value"], 0.05)

    def test_shifted_distribution_has_low_p_value(self):
        rng = np.random.default_rng(1)
        ref = rng.normal(0, 1, 300)
        cur = rng.normal(3, 1, 300)
        result = ks_test(ref, cur)
        self.assertLess(result["p_value"], 0.05)

    def test_too_few_rows_returns_none(self):
        self.assertIsNone(ks_test([1, 2], [1, 2]))


class FeatureSchemaDriftTests(SimpleTestCase):
    def test_no_change_reports_unchanged(self):
        version = MLModelVersion(feature_schema=["a", "b", "c"])
        result = assess_feature_schema(version, ["a", "b", "c"])
        self.assertFalse(result["changed"])
        self.assertEqual(result["missing_features"], [])
        self.assertEqual(result["newly_introduced_features"], [])

    def test_missing_and_added_features_detected(self):
        version = MLModelVersion(feature_schema=["a", "b", "c"])
        result = assess_feature_schema(version, ["a", "b", "d"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["missing_features"], ["c"])
        self.assertEqual(result["newly_introduced_features"], ["d"])

    def test_no_model_version_reports_all_current_as_new(self):
        result = assess_feature_schema(None, ["a"])
        self.assertEqual(result["trained_features"], [])


def _classifier_rows(n, drift=False):
    rows = []
    for i in range(n):
        prob = 0.5 + (0.3 if drift and i >= n // 2 else 0.0)
        prob = min(prob, 0.95)
        outcome = prob > 0.5
        rows.append(
            {
                "data_cutoff_date": date(2026, 1, 1) + timedelta(days=i),
                "predicted_class": prob > 0.5,
                "predicted_probability": prob,
                "outcome_class": outcome,
            }
        )
    return rows


class AssessDriftEndToEndTests(SimpleTestCase):
    def test_small_window_skips_split_half_comparison(self):
        rows = _classifier_rows(MIN_ROWS_PER_HALF)  # below 2x threshold
        version = MLModelVersion(feature_schema=list(FEATURE_COLS))
        result = assess_drift(rows, PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF, model_version=version)
        self.assertIn("note", result)
        self.assertIsNone(result["reference_period"])
        self.assertEqual(result["flags"], [])  # unchanged feature schema, and too few rows for a split-half comparison

    def test_stable_predictions_produce_no_flags(self):
        rows = _classifier_rows(MIN_ROWS_PER_HALF * 4, drift=False)
        version = MLModelVersion(feature_schema=list(FEATURE_COLS))  # matches current columns -> no schema-change flag
        result = assess_drift(rows, PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF, model_version=version)
        self.assertEqual(result["flags"], [])

    def test_shifted_predictions_are_flagged(self):
        rows = _classifier_rows(MIN_ROWS_PER_HALF * 4, drift=True)
        version = MLModelVersion(feature_schema=list(FEATURE_COLS))
        result = assess_drift(rows, PredictionSnapshot.ModelFamily.FORWARD_RETURN_RF, model_version=version)
        self.assertTrue(result["flags"])
