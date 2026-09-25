"""DB-free unit tests for the regime-tuning search primitives.

These exercise the routing / threshold / band / coverage logic on synthetic
frames so the behaviour is pinned without needing a populated database.
"""
import numpy as np
import pandas as pd
from django.test import SimpleTestCase

from market.services import regime_tuning as rt


def _frame(regimes, labels):
    return pd.DataFrame({"trend_regime": regimes, "label": labels})


class ThresholdTuningTests(SimpleTestCase):
    def test_threshold_picks_value_maximising_balanced_accuracy(self):
        # Regime 1.0: probability perfectly separates the classes at 0.5.
        n = 40
        prob = np.concatenate([np.full(n // 2, 0.2), np.full(n // 2, 0.8)])
        labels = np.concatenate([np.zeros(n // 2), np.ones(n // 2)])
        val = _frame(np.ones(n), labels)
        thresholds = rt._tune_thresholds(val, prob, [1.0])
        # Any threshold in (0.2, 0.8] gives a perfect split; the grid should
        # land on one of those, never below 0.2 or above 0.8.
        self.assertGreater(thresholds[1.0], 0.2)
        self.assertLessEqual(thresholds[1.0], 0.8)

    def test_regime_with_too_few_rows_defaults_to_half(self):
        val = _frame(np.ones(5), np.array([0, 1, 0, 1, 0]))
        prob = np.full(5, 0.6)
        thresholds = rt._tune_thresholds(val, prob, [1.0])
        self.assertEqual(thresholds[1.0], 0.5)


class BandSelectionTests(SimpleTestCase):
    def test_band_respects_minimum_coverage_floor(self):
        # Probabilities cluster tightly around the threshold, so a wide band
        # abstains on nearly everything. With a 0.5 coverage floor the search
        # must not pick a band that drops below it.
        n = 100
        rng = np.random.default_rng(0)
        prob = np.clip(0.5 + rng.normal(0, 0.02, n), 0, 1)
        labels = rng.integers(0, 2, n)
        val = _frame(np.ones(n), labels)
        thresholds = {1.0: 0.5}
        band, _, coverage, cleared = rt._select_band(
            val, prob, thresholds, min_coverage=0.5
        )
        self.assertTrue(cleared)
        self.assertGreaterEqual(coverage, 0.5)
        self.assertEqual(band, 0.0)  # only the widest-coverage band clears it

    def test_no_band_clears_floor_falls_back_to_zero(self):
        n = 30
        prob = np.full(n, 0.5)  # zero confidence everywhere
        val = _frame(np.ones(n), np.zeros(n))
        band, _, _, cleared = rt._select_band(
            val, prob, {1.0: 0.5}, min_coverage=0.9
        )
        self.assertFalse(cleared)
        self.assertEqual(band, 0.0)


class RoutingTests(SimpleTestCase):
    def test_rows_in_unmodelled_regime_stay_nan(self):
        frame = _frame(np.array([1.0, 1.0, -1.0, -1.0]), np.array([1, 0, 1, 0]))
        frame[rt.FEATURE_COLS] = 0.0
        # trend_regime is itself a feature column, so restore the routing
        # values after the blanket feature assignment above clobbered them.
        frame["trend_regime"] = np.array([1.0, 1.0, -1.0, -1.0])
        # Only a model for regime 1.0 exists.
        class _Stub:
            classes_ = [0.0, 1.0]

            def predict_proba(self, X):
                return np.column_stack([np.full(len(X), 0.3), np.full(len(X), 0.7)])

        from market.services.ml_training import fit_median_imputer

        imputer = fit_median_imputer(frame[rt.FEATURE_COLS])
        models = {1.0: {"imputer": imputer, "model": _Stub()}}
        prob = rt._predict_prob(models, frame)
        self.assertFalse(np.isnan(prob[0]))
        self.assertFalse(np.isnan(prob[1]))
        self.assertTrue(np.isnan(prob[2]))  # regime -1.0 has no model
        self.assertTrue(np.isnan(prob[3]))


class ApplyTests(SimpleTestCase):
    def test_covered_excludes_low_confidence_and_nan(self):
        frame = _frame(np.ones(4), np.array([1, 0, 1, 0]))
        prob = np.array([0.9, 0.52, np.nan, 0.1])
        thresholds = {1.0: 0.5}
        scored, y, pred, p, covered = rt._apply(frame, prob, thresholds, band=0.08)
        self.assertTrue(covered[0])       # |0.9-0.5| = 0.40 >= 0.08
        self.assertFalse(covered[1])      # |0.52-0.5| = 0.02 < 0.08
        self.assertFalse(covered[2])      # NaN -> not scored
        self.assertTrue(covered[3])       # |0.1-0.5| = 0.40 >= 0.08
        self.assertEqual(pred[0], 1)
        self.assertEqual(pred[3], 0)
