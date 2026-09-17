from unittest import mock

import numpy as np
import pandas as pd
from django.test import SimpleTestCase, TestCase

from market.services.close_learn import FEATURE_COLS, MIN_FOLD_TRAIN_ROWS
from market.services.next_close_challenger import (
    ChallengerConfig,
    _metrics,
    _selective,
    evaluate_locked_holdout,
    train_for_shadow,
)


class SelectivePolicyTests(SimpleTestCase):
    def test_shrinks_clips_and_abstains_inside_deadband(self):
        config = ChallengerConfig(shrinkage=0.5, deadband=0.01)
        result = _selective(np.array([0.01, 0.04, -1.0]), config)
        np.testing.assert_allclose(result, [0.0, 0.02, -0.12])

    def test_metrics_compare_return_mae_with_no_change_baseline(self):
        result = _metrics(np.array([0.02, -0.02]), np.array([0.01, -0.01]))
        self.assertEqual(result["skill_vs_naive"], 0.5)
        self.assertEqual(result["direction_hit_rate"], 1.0)
        self.assertEqual(result["coverage"], 1.0)


class LockedHoldoutTests(SimpleTestCase):
    def test_holdout_is_not_used_by_walk_forward_or_fit(self):
        dates = pd.date_range("2025-01-01", periods=500, freq="D")
        panel = pd.DataFrame({"date": dates, "fwd_ret_1": np.tile([0.01, -0.01], 250)})
        for col in FEATURE_COLS:
            panel[col] = 0.0

        research_eval = {
            "ok": True,
            "metrics": {"skill_vs_naive": 0.03},
            "folds": [{"skill_vs_naive": 0.1}, {"skill_vs_naive": 0.05}, {"skill_vs_naive": -0.01}],
        }

        def fake_fit_predict(train, test, config):
            self.assertLess(train["date"].max(), test["date"].min())
            return test["fwd_ret_1"].to_numpy()

        with mock.patch(
            "market.services.next_close_challenger.walk_forward_evaluate", return_value=research_eval
        ), mock.patch(
            "market.services.next_close_challenger._fit_predict", side_effect=fake_fit_predict
        ):
            result = evaluate_locked_holdout(panel)

        self.assertTrue(result["ready_for_shadow"])
        self.assertFalse(result["ready_for_production"])

    def test_tiny_improvement_does_not_clear_robustness_gate(self):
        dates = pd.date_range("2025-01-01", periods=500, freq="D")
        panel = pd.DataFrame({"date": dates, "fwd_ret_1": np.tile([0.01, -0.01], 250)})
        for col in FEATURE_COLS:
            panel[col] = 0.0
        research_eval = {
            "ok": True,
            "metrics": {"skill_vs_naive": 0.0015},
            "folds": [{"skill_vs_naive": 0.001}, {"skill_vs_naive": 0.001}, {"skill_vs_naive": 0.002}],
        }
        with mock.patch(
            "market.services.next_close_challenger.walk_forward_evaluate", return_value=research_eval
        ), mock.patch(
            "market.services.next_close_challenger._fit_predict",
            return_value=panel.tail(91)["fwd_ret_1"].to_numpy(),
        ):
            result = evaluate_locked_holdout(panel)
        self.assertFalse(result["ready_for_shadow"])


class ShadowTrainingTests(TestCase):
    @mock.patch("market.services.next_close_challenger._build_next_close_panel")
    def test_too_little_data_does_not_fit(self, panel_mock):
        panel_mock.return_value = pd.DataFrame(index=range(MIN_FOLD_TRAIN_ROWS - 1))
        self.assertIsNone(train_for_shadow())
