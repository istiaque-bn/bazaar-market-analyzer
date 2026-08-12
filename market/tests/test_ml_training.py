"""
Phase 4 — proves the ML training/evaluation correctness rules:
  1. Unknown future outcomes are dropped, never mislabeled as class 0.
  2. Walk-forward folds never let a train row's label window reach into
     the following test window (embargo).
  3. Preprocessing (median imputation) is fit on the train fold only.
  4. A model with non-positive out-of-sample skill is saved experimental
     and is never served by inference (ml_probability / next-close).
  5. Prior model artifacts are backed up, never silently overwritten.

None of these tests touch the real db.sqlite3 or data/cache/*.pkl — every
trained artifact in this file is written to a tempfile.TemporaryDirectory.
"""
import tempfile
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
from django.test import SimpleTestCase, TestCase, override_settings

from market.models import Exchange, MLModelVersion, PriceHistory, Stock
from market.services import close_learn, ml_model
from market.services.ml_training import (
    apply_imputer,
    backup_existing_model,
    fit_median_imputer,
    validate_chronological,
    walk_forward_folds,
)


def _price_df(n=120, seed=0):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2025-01-01", periods=n, freq="C", weekmask="Sun Mon Tue Wed Thu")
    closes = 100 + np.cumsum(rng.normal(0, 1, n))
    volume = np.full(n, 5000)
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": closes + 0.5,
            "low": closes - 0.5,
            "close": closes,
            "volume": volume,
            "value": closes * volume,
        }
    )


def _synthetic_labeled_panel(n=260, seed=0, feature_cols=None):
    feature_cols = feature_cols or ml_model.FEATURE_COLS
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-01", periods=n, freq="C", weekmask="Sun Mon Tue Wed Thu")
    data = {c: rng.normal(0, 1, n) for c in feature_cols}
    data["date"] = dates
    data["label"] = rng.integers(0, 2, n).astype(float)
    return pd.DataFrame(data)


class ChronologicalValidationTests(SimpleTestCase):
    def test_sorts_unsorted_dates(self):
        df = pd.DataFrame({"date": pd.to_datetime(["2026-01-03", "2026-01-01", "2026-01-02"]), "x": [3, 1, 2]})
        out = validate_chronological(df)
        self.assertEqual(list(out["x"]), [1, 2, 3])

    def test_rejects_duplicate_dates(self):
        df = pd.DataFrame({"date": pd.to_datetime(["2026-01-01", "2026-01-01"]), "x": [1, 2]})
        with self.assertRaises(ValueError):
            validate_chronological(df)

    def test_rejects_null_dates(self):
        df = pd.DataFrame({"date": pd.to_datetime(["2026-01-01", None]), "x": [1, 2]})
        with self.assertRaises(ValueError):
            validate_chronological(df)

    def test_empty_frame_is_a_noop(self):
        df = pd.DataFrame({"date": pd.to_datetime([]), "x": []})
        self.assertTrue(validate_chronological(df).empty)


class WalkForwardEmbargoTests(SimpleTestCase):
    def test_no_train_row_can_reach_into_the_test_window(self):
        """The whole point of the embargo: train_end must sit at least
        `embargo_days` before test_start, so a training label that looks
        `horizon` days into the future can never be computed from data
        that falls inside the test window."""
        dates = pd.Series(pd.bdate_range("2023-01-01", periods=400))
        folds = walk_forward_folds(dates, n_folds=5, embargo_days=14)
        self.assertGreater(len(folds), 0)
        for f in folds:
            self.assertLessEqual(f.train_end, f.test_start - pd.Timedelta(days=14))
            self.assertLess(f.test_start, f.test_end)

    def test_folds_expand_chronologically(self):
        dates = pd.Series(pd.bdate_range("2023-01-01", periods=400))
        folds = walk_forward_folds(dates, n_folds=5, embargo_days=14)
        for a, b in zip(folds, folds[1:]):
            self.assertLess(a.train_end, b.train_end)
            self.assertLessEqual(a.test_end, b.test_start.normalize() + pd.Timedelta(days=1) or b.test_start)

    def test_too_few_distinct_dates_yields_no_folds(self):
        dates = pd.Series(pd.bdate_range("2023-01-01", periods=10))
        self.assertEqual(walk_forward_folds(dates), [])


class PreprocessingFitOnTrainOnlyTests(SimpleTestCase):
    def test_imputer_fills_using_train_median_not_test_data(self):
        X_train = pd.DataFrame({"a": [1.0, 2.0, np.nan, 4.0]})  # median of observed values = 2.0
        X_test = pd.DataFrame({"a": [1000.0, 1000.0, 1000.0]})  # would drag the median way up if it leaked in

        imputer = fit_median_imputer(X_train)
        filled_train = apply_imputer(imputer, X_train)
        self.assertEqual(filled_train["a"].iloc[2], 2.0)

        # The same train-fit imputer applied to test data must not refit —
        # it just transforms using the statistic already learned from train.
        filled_test = apply_imputer(imputer, X_test)
        self.assertTrue((filled_test["a"] == 1000.0).all())


class ForwardReturnLabelLeakageTests(TestCase):
    """Direct regression test for the Phase 4 bug: rows whose 10-day
    forward return is not yet knowable must never be labeled class 0.
    TestCase (not SimpleTestCase) because _feature_frame now queries
    exchange/sector context for the relative-strength/regime features —
    harmless here (no PriceHistory fixtures exist, so context defaults to
    0/flat), but it's a real DB read either way."""

    def test_rows_near_series_end_get_no_label_not_class_zero(self):
        df = _price_df(n=100, seed=3)
        feats = ml_model._feature_frame(df)
        tail = feats.tail(ml_model.FORWARD_HORIZON_TRADING_DAYS)
        self.assertTrue(tail["fwd_ret_10"].isna().all())
        self.assertTrue(
            tail["label"].isna().all(),
            "rows with an unknown 10-day-forward outcome must stay unlabeled, not become class 0",
        )

    def test_rows_with_a_known_future_do_get_a_real_label(self):
        df = _price_df(n=100, seed=3)
        feats = ml_model._feature_frame(df)
        known = feats.iloc[: -ml_model.FORWARD_HORIZON_TRADING_DAYS]
        # Every row with a defined fwd_ret_10 must have a defined (0/1) label.
        has_fwd = known["fwd_ret_10"].notna()
        self.assertTrue(known.loc[has_fwd, "label"].notna().all())
        self.assertTrue(set(known.loc[has_fwd, "label"].unique()) <= {0.0, 1.0})


class BuildTrainingPanelExcludesUnknownFutureTests(TestCase):
    def test_panel_never_contains_the_last_horizon_days_of_a_stocks_history(self):
        stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="LEAKTEST", company_name="LeakTest", is_active=True)
        n = 150
        dates = pd.bdate_range("2025-01-01", periods=n, freq="C", weekmask="Sun Mon Tue Wed Thu")
        rng = np.random.default_rng(7)
        closes = 100 + np.cumsum(rng.normal(0, 1, n))
        PriceHistory.objects.bulk_create(
            PriceHistory(
                stock=stock, date=d.date(), open=float(c), high=float(c) + 0.5, low=float(c) - 0.5, close=float(c), volume=5000
            )
            for d, c in zip(dates, closes)
        )

        panel = ml_model.build_training_panel(Exchange.DSE)
        self.assertFalse(panel.empty)
        last_unknown = {pd.Timestamp(d) for d in dates[-ml_model.FORWARD_HORIZON_TRADING_DAYS :]}
        self.assertTrue(last_unknown.isdisjoint(set(panel["date"])))


class DeploymentGateTests(TestCase):
    """Isolated tests of the gate logic: _final_fit_and_save() decides
    active/experimental purely from the skill_vs_naive it's handed, and
    ml_probability() must refuse to use anything not verified active."""

    def _empty_eval(self, skill):
        return {"skill_vs_naive": skill, "folds": [], "model_metrics": {"n": 0}, "baseline_metrics": {}, "skill_vs_baseline": {}}

    def test_non_positive_skill_is_saved_experimental_and_inactive(self):
        panel = _synthetic_labeled_panel(seed=1)
        with tempfile.TemporaryDirectory() as td:
            result = ml_model._final_fit_and_save(
                panel, self._empty_eval(-0.02), exchange_scope="combined", model_path=Path(td) / "m.pkl"
            )
        self.assertEqual(result["status"], "experimental")
        self.assertFalse(result["is_active"])
        version = MLModelVersion.objects.get(model_name=ml_model.MODEL_NAME, version=result["version"])
        self.assertFalse(version.is_active)
        self.assertEqual(version.status, "experimental")

    def test_zero_skill_is_not_active_either(self):
        panel = _synthetic_labeled_panel(seed=2)
        with tempfile.TemporaryDirectory() as td:
            result = ml_model._final_fit_and_save(
                panel, self._empty_eval(0.0), exchange_scope="combined", model_path=Path(td) / "m.pkl"
            )
        self.assertFalse(result["is_active"])

    def test_positive_skill_is_saved_active(self):
        panel = _synthetic_labeled_panel(seed=3)
        with tempfile.TemporaryDirectory() as td:
            result = ml_model._final_fit_and_save(
                panel, self._empty_eval(0.05), exchange_scope="combined", model_path=Path(td) / "m.pkl"
            )
        self.assertEqual(result["status"], "active")
        self.assertTrue(result["is_active"])

    def test_experimental_model_is_never_served_by_ml_probability(self):
        panel = _synthetic_labeled_panel(seed=4)
        with tempfile.TemporaryDirectory() as td:
            model_path = Path(td) / "combined.pkl"
            ml_model._final_fit_and_save(panel, self._empty_eval(-0.1), exchange_scope="combined", model_path=model_path)
            with mock.patch.object(ml_model, "MODEL_PATH", model_path):
                self.assertIsNone(ml_model.ml_probability(_price_df(n=120)))

    def test_active_model_is_served_by_ml_probability(self):
        panel = _synthetic_labeled_panel(seed=5)
        with tempfile.TemporaryDirectory() as td:
            model_path = Path(td) / "combined.pkl"
            ml_model._final_fit_and_save(panel, self._empty_eval(0.1), exchange_scope="combined", model_path=model_path)
            with mock.patch.object(ml_model, "MODEL_PATH", model_path):
                prob = ml_model.ml_probability(_price_df(n=120))
        self.assertIsNotNone(prob)
        self.assertTrue(0.0 <= prob <= 1.0)

    def test_db_downgrade_after_train_time_active_stops_inference_without_retrain(self):
        """A model can be marked active at train time and later downgraded
        (e.g. by live-skill monitoring) without the pickle file changing —
        the DB row, not the file, is the live source of truth."""
        panel = _synthetic_labeled_panel(seed=6)
        with tempfile.TemporaryDirectory() as td:
            model_path = Path(td) / "combined.pkl"
            result = ml_model._final_fit_and_save(panel, self._empty_eval(0.1), exchange_scope="combined", model_path=model_path)
            with mock.patch.object(ml_model, "MODEL_PATH", model_path):
                self.assertIsNotNone(ml_model.ml_probability(_price_df(n=120)))
                MLModelVersion.objects.filter(model_name=ml_model.MODEL_NAME, version=result["version"]).update(is_active=False)
                self.assertIsNone(ml_model.ml_probability(_price_df(n=120)))


class BackupPreservationTests(SimpleTestCase):
    def test_existing_model_is_copied_aside_before_being_overwritten(self):
        with tempfile.TemporaryDirectory() as td:
            model_path = Path(td) / "model.pkl"
            model_path.write_bytes(b"original-bytes")
            backup_path = backup_existing_model(model_path)
            self.assertIsNotNone(backup_path)
            self.assertTrue(Path(backup_path).exists())
            self.assertEqual(Path(backup_path).read_bytes(), b"original-bytes")
            # The original itself is untouched by the backup step (the
            # caller overwrites it afterwards, not backup_existing_model).
            self.assertEqual(model_path.read_bytes(), b"original-bytes")

    def test_no_existing_model_means_no_backup(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(backup_existing_model(Path(td) / "does_not_exist.pkl"))


class NextClosePanelLabelLeakageTests(TestCase):
    def test_panel_excludes_the_final_bar_of_each_stock(self):
        stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="NCLEAK", company_name="NCLeak", is_active=True)
        n = 100
        dates = pd.bdate_range("2025-01-01", periods=n, freq="C", weekmask="Sun Mon Tue Wed Thu")
        rng = np.random.default_rng(9)
        closes = 100 + np.cumsum(rng.normal(0, 1, n))
        PriceHistory.objects.bulk_create(
            PriceHistory(
                stock=stock, date=d.date(), open=float(c), high=float(c) + 0.5, low=float(c) - 0.5, close=float(c), volume=5000
            )
            for d, c in zip(dates, closes)
        )
        panel = close_learn._build_next_close_panel(Exchange.DSE, limit_stocks=10)
        self.assertFalse(panel.empty)
        self.assertNotIn(pd.Timestamp(dates[-1]), set(panel["date"]))


def _bulk_create_stocks(exchange, count, n_bars=150, prefix="X", seed_base=100):
    stocks = []
    for i in range(count):
        stock = Stock.objects.create(exchange=exchange, trading_code=f"{prefix}{i:03d}", company_name=f"{prefix}{i:03d}", is_active=True)
        rng = np.random.default_rng(seed_base + i)
        dates = pd.bdate_range("2024-06-01", periods=n_bars, freq="C", weekmask="Sun Mon Tue Wed Thu")
        closes = 100 + np.cumsum(rng.normal(0.05, 1.2, n_bars))
        closes = np.clip(closes, 5, None)
        PriceHistory.objects.bulk_create(
            PriceHistory(
                stock=stock, date=d.date(), open=float(c), high=float(c) + 0.5, low=float(c) - 0.5, close=float(c), volume=5000
            )
            for d, c in zip(dates, closes)
        )
        stocks.append(stock)
    return stocks


class WalkForwardEvaluateEndToEndTests(TestCase):
    """Exercises market.services.ml_model.walk_forward_evaluate() and
    _justify_combining() against a real (if small) DSE+CSE panel built
    through build_training_panel(), proving the pieces glue together —
    not just the isolated units above."""

    def setUp(self):
        _bulk_create_stocks(Exchange.DSE, 20, prefix="D")
        _bulk_create_stocks(Exchange.CSE, 20, prefix="C")

    def test_per_exchange_walk_forward_produces_folds_and_baselines(self):
        dse_panel = ml_model.build_training_panel(Exchange.DSE)
        self.assertGreaterEqual(len(dse_panel), ml_model.MIN_PANEL_ROWS)
        result = ml_model.walk_forward_evaluate(dse_panel)
        self.assertTrue(result["ok"])
        self.assertGreater(result["n_folds_used"], 0)
        for name in ("majority_class", "zero_return", "persistence", "simple_market"):
            self.assertIn(name, result["baseline_metrics"])
            self.assertIn(name, result["skill_vs_baseline"])
        self.assertIn("balanced_accuracy", result["model_metrics"])

    def test_train_model_end_to_end_writes_versions_without_touching_real_cache(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_combined = Path(td) / "combined.pkl"
            tmp_by_exchange = {
                Exchange.DSE: Path(td) / "dse.pkl",
                Exchange.CSE: Path(td) / "cse.pkl",
            }
            with mock.patch.object(ml_model, "MODEL_PATH", tmp_combined), mock.patch.object(
                ml_model, "MODEL_PATH_BY_EXCHANGE", tmp_by_exchange
            ):
                result = ml_model.train_model(limit_stocks=20)

            self.assertTrue(result["ok"])
            evaluation = result["evaluation"]
            self.assertTrue(evaluation["dse"]["ok"])
            self.assertTrue(evaluation["cse"]["ok"])
            self.assertIn(evaluation["combine_justified"], (True, False))
            self.assertTrue(result["deployed"], "at least one scope must have been deployed (combined or per-exchange)")

            for scope_result in result["deployed"].values():
                self.assertIn(scope_result["status"], ("active", "experimental"))
                path = Path(scope_result["path"])
                self.assertTrue(path.exists())
                self.assertTrue(path.is_relative_to(td))

            versions = MLModelVersion.objects.filter(model_name=ml_model.MODEL_NAME)
            self.assertGreater(versions.count(), 0)
            for v in versions:
                self.assertTrue(v.fold_metadata, "fold dates/sample-counts/class-balance must be tracked")
                self.assertEqual(v.feature_schema, ml_model.FEATURE_COLS)
                self.assertIsNotNone(v.data_cutoff)


class NextCloseTrainEndToEndTests(TestCase):
    def setUp(self):
        _bulk_create_stocks(Exchange.DSE, 20, n_bars=140, prefix="ND")
        _bulk_create_stocks(Exchange.CSE, 20, n_bars=140, prefix="NC")

    def test_train_next_close_model_end_to_end_without_touching_real_cache(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_combined = Path(td) / "combined.pkl"
            tmp_by_exchange = {
                Exchange.DSE: Path(td) / "dse.pkl",
                Exchange.CSE: Path(td) / "cse.pkl",
            }
            with mock.patch.object(close_learn, "MODEL_PATH", tmp_combined), mock.patch.object(
                close_learn, "MODEL_PATH_BY_EXCHANGE", tmp_by_exchange
            ):
                result = close_learn.train_next_close_model(limit_stocks=20)

            self.assertTrue(result["ok"])
            self.assertTrue(result["deployed"])
            for scope_result in result["deployed"].values():
                path = Path(scope_result["path"])
                self.assertTrue(path.exists())
                self.assertTrue(path.is_relative_to(td))

            versions = MLModelVersion.objects.filter(model_name=close_learn.MODEL_NAME)
            self.assertGreater(versions.count(), 0)


class LiveSkillDowngradeTests(TestCase):
    def _make_active_version(self):
        return MLModelVersion.objects.create(
            model_name=close_learn.MODEL_NAME,
            version="20260101-000000",
            exchange_scope="combined",
            status=close_learn.STATUS_ACTIVE,
            is_active=True,
            data_cutoff="2026-01-01",
            train_rows=500,
        )

    def test_enough_settles_with_non_positive_skill_downgrades_active_model(self):
        version = self._make_active_version()
        close_learn._maybe_downgrade_on_live_skill({"n": 60, "skill_vs_naive": -0.05})
        version.refresh_from_db()
        self.assertFalse(version.is_active)
        self.assertEqual(version.status, close_learn.STATUS_EXPERIMENTAL)

    def test_too_few_settles_leaves_model_active(self):
        version = self._make_active_version()
        close_learn._maybe_downgrade_on_live_skill({"n": 5, "skill_vs_naive": -0.5})
        version.refresh_from_db()
        self.assertTrue(version.is_active)

    def test_positive_live_skill_leaves_model_active(self):
        version = self._make_active_version()
        close_learn._maybe_downgrade_on_live_skill({"n": 200, "skill_vs_naive": 0.1})
        version.refresh_from_db()
        self.assertTrue(version.is_active)


class ModelTrainingConcurrencyCapTests(TestCase):
    """XGBoost/RandomForest fits used to hard-code n_jobs=-1 (every core) —
    now read settings.ML_MAX_WORKERS so one training task can't saturate a
    small VPS against Gunicorn/DB/Redis on the same box. Only the thread
    count changes; asserting the constructor kwarg is enough to prove the
    plumbing without needing to compare model output."""

    @override_settings(ML_MAX_WORKERS=3)
    def test_ml_model_walk_forward_fit_respects_ml_max_workers(self):
        _bulk_create_stocks(Exchange.DSE, 20, prefix="NJ")
        panel = ml_model.build_training_panel(Exchange.DSE)
        real_xgb = ml_model.XGBClassifier
        with mock.patch("market.services.ml_model.XGBClassifier", side_effect=lambda **kw: real_xgb(**kw)) as mock_xgb:
            result = ml_model.walk_forward_evaluate(panel)
        self.assertTrue(result["ok"])
        self.assertTrue(mock_xgb.called)
        for call in mock_xgb.call_args_list:
            self.assertEqual(call.kwargs["n_jobs"], 3)

    @override_settings(ML_MAX_WORKERS=3)
    def test_next_close_direction_models_respect_ml_max_workers(self):
        with mock.patch("market.services.close_learn.RandomForestClassifier") as mock_rf, \
                mock.patch("market.services.close_learn.XGBClassifier") as mock_xgb:
            close_learn._direction_model("random_forest")
            close_learn._direction_model("xgboost")
        self.assertEqual(mock_rf.call_args.kwargs["n_jobs"], 3)
        self.assertEqual(mock_xgb.call_args.kwargs["n_jobs"], 3)

    @override_settings(ML_MAX_WORKERS=3)
    def test_zero_inflated_next_close_models_respect_ml_max_workers(self):
        with mock.patch("market.services.close_learn.RandomForestClassifier") as mock_rf_cls, \
                mock.patch("market.services.close_learn.RandomForestRegressor") as mock_rf_reg:
            mock_rf_cls.return_value.fit.return_value = None
            mock_rf_reg.return_value.fit.return_value = None
            X = pd.DataFrame({"f1": [0.1, 0.2, 0.3, 0.4]})
            y = np.array([0.0, 0.01, -0.02, 0.0])
            close_learn._fit_zero_inflated_next_close(X, y)
        self.assertEqual(mock_rf_cls.call_args.kwargs["n_jobs"], 3)
        self.assertEqual(mock_rf_reg.call_args.kwargs["n_jobs"], 3)
