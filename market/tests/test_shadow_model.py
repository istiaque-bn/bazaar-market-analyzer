from datetime import date, timedelta
from unittest.mock import patch

import numpy as np
import pandas as pd
from django.test import TestCase

from market.models import Exchange, PriceHistory, ShadowForecast, Stock
from market.services.close_learn import FEATURE_COLS, MIN_FOLD_TRAIN_ROWS
from market.services.shadow_model import (
    NAME,
    REGRESSION_NAME,
    _train_shadow_regression,
    render_shadow_comparison_text,
    render_shadow_report_text,
    run_shadow_cycle,
    shadow_report,
)


def _mk_row(stock, *, target_date, last_close, predicted_close, actual_close):
    return ShadowForecast.objects.create(
        stock=stock,
        as_of=target_date - timedelta(days=1),
        target_date=target_date,
        last_close=last_close,
        predicted_close=predicted_close,
        predicted_return=(predicted_close - last_close) / last_close,
        candidate_name=NAME,
        actual_close=actual_close,
        settled_at=None,
    )


class ShadowReportMetricsTests(TestCase):
    def setUp(self):
        self.stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="SHDW", company_name="Shadow Co")

    def test_no_rows_reports_zero_and_no_trend(self):
        result = shadow_report()
        self.assertEqual(result, {"n": 0, "trend": None})

    def test_skill_and_direction_computed_from_settled_rows(self):
        today = date.today()
        # actual always 102 vs last_close 100 (naive error = 2); model
        # error = 1 -> skill = 1 - 1/2 = 0.5. Direction is correctly "up".
        for i in range(5):
            _mk_row(self.stock, target_date=today - timedelta(days=i), last_close=100, predicted_close=101, actual_close=102)
        result = shadow_report()
        self.assertEqual(result["n"], 5)
        self.assertAlmostEqual(result["skill"], 0.5)
        self.assertEqual(result["direction"], 1.0)

    def test_below_trend_threshold_reports_no_trend(self):
        today = date.today()
        for i in range(10):
            _mk_row(self.stock, target_date=today - timedelta(days=i), last_close=100, predicted_close=101, actual_close=102)
        result = shadow_report()
        self.assertIsNone(result["trend"])

    def test_recent_half_better_than_prior_half_is_improving(self):
        today = date.today()
        # Most recent 20 rows: accurate (skill 0.5). Older 20 rows (well
        # separated in time): inaccurate (skill -1). order_by("-target_date")
        # puts the accurate, more-recent rows first -> recent_skill > prior_skill.
        for i in range(20):
            _mk_row(self.stock, target_date=today - timedelta(days=i), last_close=100, predicted_close=101, actual_close=102)
        for i in range(20):
            _mk_row(self.stock, target_date=today - timedelta(days=40 + i), last_close=100, predicted_close=106, actual_close=102)
        result = shadow_report()
        self.assertEqual(result["n"], 40)
        self.assertEqual(result["trend"], "improving")

    def test_recent_half_worse_than_prior_half_is_declining(self):
        today = date.today()
        for i in range(20):
            _mk_row(self.stock, target_date=today - timedelta(days=i), last_close=100, predicted_close=106, actual_close=102)
        for i in range(20):
            _mk_row(self.stock, target_date=today - timedelta(days=40 + i), last_close=100, predicted_close=101, actual_close=102)
        result = shadow_report()
        self.assertEqual(result["trend"], "declining")

    def test_similar_halves_are_stable(self):
        today = date.today()
        for i in range(40):
            _mk_row(self.stock, target_date=today - timedelta(days=i), last_close=100, predicted_close=101, actual_close=102)
        result = shadow_report()
        self.assertEqual(result["trend"], "stable")


class ShadowReportTextTests(TestCase):
    def test_no_data_is_plain_language_not_a_bare_number(self):
        text = render_shadow_report_text({"n": 0, "trend": None})
        self.assertIn("No completed price checks yet", text)
        self.assertIn("never changes real forecasts", text)
        # Never a bare-numbers dump: no raw metric labels leak into the text.
        self.assertNotIn("MAE", text)
        self.assertNotIn("Skill vs naive", text)

    def test_positive_skill_is_framed_as_better_than_naive(self):
        text = render_shadow_report_text({"n": 100, "mae": 0.8, "naive_mae": 1.0, "skill": 0.2, "direction": 0.55, "trend": "improving"})
        self.assertIn("closer to the real price", text)
        self.assertIn("Trend: Improving", text)
        self.assertIn("55 times out of 100", text)
        self.assertNotIn("MAE", text)

    def test_negative_skill_is_framed_as_worse_than_naive(self):
        text = render_shadow_report_text({"n": 100, "mae": 1.2, "naive_mae": 1.0, "skill": -0.1955, "direction": 0.33, "trend": "declining"})
        self.assertIn("further from the real price", text)
        self.assertIn("doing worse than doing nothing", text)
        self.assertIn("Trend: Declining", text)

    def test_unknown_trend_falls_back_gracefully(self):
        text = render_shadow_report_text({"n": 10, "mae": 1.0, "naive_mae": 1.0, "skill": 0.0, "direction": None, "trend": None})
        self.assertIn("Not enough history yet", text)


class ShadowComparisonTextTests(TestCase):
    def test_both_candidates_appear_with_labels_and_shared_disclaimer(self):
        reports = {
            NAME: {"n": 50, "mae": 1.0, "naive_mae": 1.0, "skill": 0.1, "direction": 0.5, "trend": "stable"},
            REGRESSION_NAME: {"n": 60, "mae": 1.0, "naive_mae": 1.0, "skill": -0.02, "direction": 0.4, "trend": "improving"},
        }
        text = render_shadow_comparison_text(reports)
        self.assertIn("Analogue + ML blend", text)
        self.assertIn("Direct regression", text)
        self.assertIn("Trend: Holding steady", text)
        self.assertIn("Trend: Improving", text)
        self.assertIn("never changes real forecasts", text)


def _synthetic_panel(n: int) -> pd.DataFrame:
    """Enough rows/columns to exercise _train_shadow_regression's real
    fit path (median imputer + _fit_zero_inflated_next_close, both
    already covered for correctness in test_ml_training.py) without
    needing real price history — this only checks the new orchestration
    wiring, not the modeling math itself."""
    rng = np.random.default_rng(42)
    data = {c: rng.normal(size=n) for c in FEATURE_COLS}
    data["fwd_ret_1"] = rng.normal(scale=0.02, size=n)
    return pd.DataFrame(data)


class TrainShadowRegressionTests(TestCase):
    @patch("market.services.shadow_model._build_next_close_panel")
    def test_returns_none_on_empty_panel(self, mock_panel):
        mock_panel.return_value = pd.DataFrame()
        self.assertIsNone(_train_shadow_regression())

    @patch("market.services.shadow_model._build_next_close_panel")
    def test_returns_none_when_panel_too_small(self, mock_panel):
        mock_panel.return_value = _synthetic_panel(MIN_FOLD_TRAIN_ROWS - 1)
        self.assertIsNone(_train_shadow_regression())

    @patch("market.services.shadow_model._build_next_close_panel")
    def test_fits_end_to_end_on_a_large_enough_panel(self, mock_panel):
        mock_panel.return_value = _synthetic_panel(MIN_FOLD_TRAIN_ROWS + 50)
        result = _train_shadow_regression()
        self.assertIsNotNone(result)
        imputer, classifier, regressor = result
        self.assertTrue(hasattr(classifier, "predict_proba"))
        self.assertTrue(hasattr(regressor, "predict"))


class RunShadowCycleRegressionTests(TestCase):
    def setUp(self):
        self.stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="SHDR", company_name="Shadow Regression Co")
        end = date.today()
        for i in range(60):
            day = end - timedelta(days=60 - i)
            close = 100 + i * 0.1
            PriceHistory.objects.create(stock=self.stock, date=day, open=close, high=close + 1, low=close - 1, close=close, volume=1000)

    @patch("market.services.shadow_model.apply_imputer", side_effect=lambda imputer, X: X)
    @patch("market.services.shadow_model.liquid_stock_ids")
    @patch("market.services.shadow_model._predict_zero_inflated")
    @patch("market.services.shadow_model._feature_row")
    @patch("market.services.shadow_model._train_shadow_regression")
    def test_creates_regression_forecast_when_training_succeeds(self, mock_train, mock_feature_row, mock_predict, mock_liquid, mock_impute):
        mock_train.return_value = ("imputer", "classifier", "regressor")
        mock_feature_row.return_value = {c: 0.0 for c in FEATURE_COLS}
        mock_predict.return_value = np.array([0.02])
        mock_liquid.return_value = {self.stock.id}

        result = run_shadow_cycle(as_of=date.today())

        self.assertEqual(result["created_regression"], 1)
        fc = ShadowForecast.objects.get(candidate_name=REGRESSION_NAME)
        self.assertAlmostEqual(fc.predicted_return, 0.02)
        self.assertAlmostEqual(fc.predicted_close, fc.last_close * 1.02, places=2)

    @patch("market.services.shadow_model.liquid_stock_ids")
    @patch("market.services.shadow_model._train_shadow_regression")
    def test_skips_regression_candidate_when_training_returns_none(self, mock_train, mock_liquid):
        mock_train.return_value = None
        mock_liquid.return_value = {self.stock.id}

        result = run_shadow_cycle(as_of=date.today())

        self.assertEqual(result["created_regression"], 0)
        self.assertFalse(ShadowForecast.objects.filter(candidate_name=REGRESSION_NAME).exists())

    @patch("market.services.shadow_model.apply_imputer", side_effect=lambda imputer, X: X)
    @patch("market.services.shadow_model.liquid_stock_ids")
    @patch("market.services.shadow_model._predict_zero_inflated")
    @patch("market.services.shadow_model._feature_row")
    @patch("market.services.shadow_model._train_shadow_regression")
    def test_never_duplicates_an_existing_regression_forecast(self, mock_train, mock_feature_row, mock_predict, mock_liquid, mock_impute):
        mock_train.return_value = ("imputer", "classifier", "regressor")
        mock_feature_row.return_value = {c: 0.0 for c in FEATURE_COLS}
        mock_predict.return_value = np.array([0.02])
        mock_liquid.return_value = {self.stock.id}

        run_shadow_cycle(as_of=date.today())
        run_shadow_cycle(as_of=date.today())

        self.assertEqual(ShadowForecast.objects.filter(candidate_name=REGRESSION_NAME).count(), 1)
