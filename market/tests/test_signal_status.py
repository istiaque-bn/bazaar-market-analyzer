from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from market.models import (
    AnalysisResult,
    CloseLearnState,
    Exchange,
    MLModelVersion,
    NextDayCloseForecast,
    SignalAction,
    Stock,
    TechnicalSnapshot,
)
from market.services.signal_status import (
    MIN_CLOSE_LEARN_SETTLED,
    MIN_ML_TRAIN_ROWS,
    close_learn_edge_status,
    data_freshness,
    evaluate_edge,
    invalidation_condition,
    liquidity_note,
    ml_model_status,
    signal_composition,
    signal_status,
)


def _mk_ml_version(**overrides):
    defaults = dict(
        model_name="forward_return_rf",
        version="20260101-000000",
        exchange_scope="combined",
        status="active",
        is_active=True,
        data_cutoff=timezone.localdate(),
        train_rows=MIN_ML_TRAIN_ROWS,
        metrics={"skill_vs_baseline": {"majority_class": 0.05}, "model": {"direction_hit_rate": 0.6}},
        fold_metadata=[{"test_start": "2026-01-01", "test_end": "2026-02-01"}],
    )
    defaults.update(overrides)
    return MLModelVersion.objects.create(**defaults)


class MlModelStatusTests(TestCase):
    def test_no_version_on_record_is_none_and_no_edge(self):
        status = ml_model_status(Exchange.DSE)
        self.assertEqual(status["status"], "none")
        self.assertFalse(status["deployed"])
        self.assertFalse(status["has_edge"])

    def test_active_with_positive_skill_and_enough_rows_has_edge(self):
        _mk_ml_version()
        status = ml_model_status(Exchange.DSE)
        self.assertTrue(status["deployed"])
        self.assertTrue(status["has_edge"])
        self.assertEqual(status["skill_vs_naive"], 0.05)
        self.assertEqual(status["direction_hit_rate"], 0.6)
        self.assertEqual(status["last_evaluation_period"], {"start": "2026-01-01", "end": "2026-02-01"})

    def test_active_but_non_positive_skill_has_no_edge(self):
        """is_active is set at train time; signal_status must independently
        re-check skill > 0 rather than blindly trusting the flag, so a
        version that was somehow marked active with non-positive skill
        (e.g. a bug elsewhere, or a post-hoc downgrade) still can't claim
        an edge."""
        _mk_ml_version(metrics={"skill_vs_baseline": {"majority_class": -0.01}, "model": {}})
        status = ml_model_status(Exchange.DSE)
        self.assertTrue(status["deployed"])
        self.assertFalse(status["has_edge"])

    def test_active_but_under_sampled_has_no_edge(self):
        _mk_ml_version(train_rows=MIN_ML_TRAIN_ROWS - 1)
        status = ml_model_status(Exchange.DSE)
        self.assertFalse(status["has_edge"])

    def test_inactive_experimental_has_no_edge(self):
        _mk_ml_version(status="experimental", is_active=False, metrics={"skill_vs_baseline": {"majority_class": 0.1}})
        status = ml_model_status(Exchange.DSE)
        self.assertFalse(status["deployed"])
        self.assertFalse(status["has_edge"])
        self.assertEqual(status["status"], "experimental")

    def test_prefers_active_combined_over_per_exchange(self):
        _mk_ml_version(exchange_scope="combined", version="v-combined")
        _mk_ml_version(exchange_scope=Exchange.DSE, version="v-dse", metrics={"skill_vs_baseline": {"majority_class": 0.9}})
        status = ml_model_status(Exchange.DSE)
        self.assertEqual(status["exchange_scope"], "combined")

    def test_falls_back_to_active_per_exchange_when_combined_inactive(self):
        _mk_ml_version(exchange_scope="combined", is_active=False, status="experimental", version="v-combined")
        _mk_ml_version(exchange_scope=Exchange.DSE, version="v-dse")
        status = ml_model_status(Exchange.DSE)
        self.assertEqual(status["exchange_scope"], Exchange.DSE)
        self.assertTrue(status["has_edge"])


class CloseLearnEdgeStatusTests(TestCase):
    def _settle_forecasts(self, n, *, skill_positive: bool):
        # Spread rows across multiple stocks (unique_together on stock+
        # target_date) but keep every target_date within the last week, so
        # they all land inside close_learn_edge_status's scoring window
        # regardless of how large `n` is.
        today = timezone.localdate()
        window = 7
        for i in range(n):
            layer = i // window
            day_offset = i % window
            stock, _ = Stock.objects.get_or_create(
                exchange=Exchange.DSE,
                trading_code=f"CLF{layer}",
                defaults={"company_name": "Close Learn Fixture"},
            )
            last_close = 100.0
            # skill_positive: predicted return closely tracks actual (beats naive=0)
            actual_close = last_close * 1.01 if i % 2 == 0 else last_close * 0.99
            predicted_return = (actual_close / last_close - 1) if skill_positive else 0.05
            target_date = today - timedelta(days=day_offset + 1)
            NextDayCloseForecast.objects.create(
                stock=stock,
                as_of=target_date - timedelta(days=1),
                target_date=target_date,
                last_close=last_close,
                predicted_close=last_close * (1 + predicted_return),
                predicted_return=predicted_return,
                actual_close=actual_close,
                settled_at=timezone.now(),
            )

    def test_no_settled_forecasts_has_no_edge(self):
        status = close_learn_edge_status()
        self.assertEqual(status["n"], 0)
        self.assertFalse(status["has_edge"])

    def test_enough_settled_with_positive_skill_has_edge(self):
        self._settle_forecasts(MIN_CLOSE_LEARN_SETTLED + 5, skill_positive=True)
        status = close_learn_edge_status()
        self.assertGreaterEqual(status["n"], MIN_CLOSE_LEARN_SETTLED)
        self.assertTrue(status["has_edge"])
        self.assertGreater(status["skill_vs_naive"], 0)

    def test_under_sampled_has_no_edge_even_with_good_predictions(self):
        self._settle_forecasts(MIN_CLOSE_LEARN_SETTLED - 5, skill_positive=True)
        status = close_learn_edge_status()
        self.assertLess(status["n"], MIN_CLOSE_LEARN_SETTLED)
        self.assertFalse(status["has_edge"])

    def test_enough_samples_but_negative_skill_has_no_edge(self):
        self._settle_forecasts(MIN_CLOSE_LEARN_SETTLED + 5, skill_positive=False)
        status = close_learn_edge_status()
        self.assertFalse(status["has_edge"])


class EvaluateEdgeTests(TestCase):
    def test_neither_has_edge_gives_false_with_reasons(self):
        ml = {"has_edge": False, "deployed": False, "skill_vs_naive": None, "train_rows": 0, "baseline": "x"}
        close = {"has_edge": False, "n": 3, "skill_vs_naive": None}
        has_edge, reason = evaluate_edge(ml, close)
        self.assertFalse(has_edge)
        self.assertIn("No demonstrated predictive edge", reason)
        self.assertIn("no up/down classifier is currently deployed", reason)
        self.assertIn("only 3 next-close forecasts have settled", reason)

    def test_ml_edge_alone_is_sufficient(self):
        ml = {"has_edge": True, "deployed": True, "skill_vs_naive": 0.1, "train_rows": 500, "baseline": "x"}
        close = {"has_edge": False, "n": 0, "skill_vs_naive": None}
        has_edge, reason = evaluate_edge(ml, close)
        self.assertTrue(has_edge)
        self.assertIn("Up/down classifier", reason)

    def test_close_learn_edge_alone_is_sufficient(self):
        ml = {"has_edge": False, "deployed": False, "skill_vs_naive": None, "train_rows": 0, "baseline": "x"}
        close = {"has_edge": True, "n": 50, "skill_vs_naive": 0.2}
        has_edge, reason = evaluate_edge(ml, close)
        self.assertTrue(has_edge)
        self.assertIn("Next-close learner", reason)

    def test_both_have_edge(self):
        ml = {"has_edge": True, "deployed": True, "skill_vs_naive": 0.1, "train_rows": 500, "baseline": "x"}
        close = {"has_edge": True, "n": 50, "skill_vs_naive": 0.2}
        has_edge, reason = evaluate_edge(ml, close)
        self.assertTrue(has_edge)
        self.assertIn("Both", reason)


class DataFreshnessTests(TestCase):
    def setUp(self):
        self.stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="FRESH", company_name="Freshness Co")

    def test_recent_analysis_is_not_stale(self):
        analysis = AnalysisResult.objects.create(stock=self.stock, as_of=timezone.localdate())
        result = data_freshness(analysis, self.stock)
        self.assertFalse(result["is_stale"])
        self.assertEqual(result["age_days"], 0)

    def test_old_analysis_is_stale(self):
        old_date = timezone.localdate() - timedelta(days=10)
        analysis = AnalysisResult.objects.create(stock=self.stock, as_of=old_date)
        result = data_freshness(analysis, self.stock)
        self.assertTrue(result["is_stale"])
        self.assertEqual(result["age_days"], 10)

    def test_no_analysis_and_no_prices_is_stale_with_no_cutoff(self):
        result = data_freshness(None, self.stock)
        self.assertIsNone(result["data_cutoff"])
        self.assertTrue(result["is_stale"])


class LiquidityNoteTests(TestCase):
    def test_no_volume_is_unknown(self):
        stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="LQ1", last_volume=None)
        self.assertEqual(liquidity_note(stock)["label"], "unknown")

    def test_thin_moderate_active_thresholds(self):
        thin = Stock.objects.create(exchange=Exchange.DSE, trading_code="LQ2", last_volume=500)
        moderate = Stock.objects.create(exchange=Exchange.DSE, trading_code="LQ3", last_volume=50_000)
        active = Stock.objects.create(exchange=Exchange.DSE, trading_code="LQ4", last_volume=500_000)
        self.assertEqual(liquidity_note(thin)["label"], "thin")
        self.assertEqual(liquidity_note(moderate)["label"], "moderate")
        self.assertEqual(liquidity_note(active)["label"], "active")
        for s in (thin, moderate, active):
            self.assertIn("not a verified liquidity", liquidity_note(s)["note"])


class InvalidationConditionTests(TestCase):
    def setUp(self):
        self.stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="INV")

    def test_no_analysis(self):
        self.assertEqual(invalidation_condition(None, None), "No active signal to invalidate.")

    def test_buy_with_support_mentions_support(self):
        analysis = AnalysisResult(stock=self.stock, as_of=timezone.localdate(), action=SignalAction.BUY)
        tech = TechnicalSnapshot(stock=self.stock, as_of=timezone.localdate(), support=123.45)
        text = invalidation_condition(analysis, tech)
        self.assertIn("123.45", text)
        self.assertIn("support", text)

    def test_sell_with_resistance_mentions_resistance(self):
        analysis = AnalysisResult(stock=self.stock, as_of=timezone.localdate(), action=SignalAction.SELL)
        tech = TechnicalSnapshot(stock=self.stock, as_of=timezone.localdate(), resistance=200.0)
        text = invalidation_condition(analysis, tech)
        self.assertIn("200.0", text)
        self.assertIn("resistance", text)

    def test_hold_is_neutral(self):
        analysis = AnalysisResult(stock=self.stock, as_of=timezone.localdate(), action=SignalAction.HOLD)
        text = invalidation_condition(analysis, None)
        self.assertIn("Neutral read", text)


class SignalCompositionTests(TestCase):
    def test_no_ml_no_pe_is_technical_only(self):
        stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="COMP1", pe_ratio=None)
        comp = signal_composition(stock, None)
        self.assertNotIn("forward-return ML classifier", comp["technical_inputs"])
        self.assertEqual(comp["fundamental_inputs"], [])
        self.assertIn("safety, liquidity, or governance certification", comp["note"])

    def test_ml_score_present_adds_ml_input(self):
        stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="COMP2")
        analysis = AnalysisResult(stock=stock, as_of=timezone.localdate(), ml_score=55.0)
        comp = signal_composition(stock, analysis)
        self.assertIn("forward-return ML classifier", comp["technical_inputs"])

    def test_pe_ratio_present_adds_fundamental_input(self):
        stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="COMP3", pe_ratio=12.5)
        comp = signal_composition(stock, None)
        self.assertTrue(comp["fundamental_inputs"])


class SignalStatusIntegrationTests(TestCase):
    """signal_status() end-to-end doesn't crash and its has_edge matches
    evaluate_edge() given the components it fetched."""

    def test_combines_components_consistently(self):
        stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="COMBO", last_volume=20_000)
        analysis = AnalysisResult.objects.create(stock=stock, as_of=timezone.localdate(), action=SignalAction.WATCH)
        status = signal_status(stock, analysis)
        expected_has_edge, expected_reason = evaluate_edge(status["ml_model"], status["next_close_model"])
        self.assertEqual(status["has_edge"], expected_has_edge)
        self.assertEqual(status["edge_reason"], expected_reason)
        self.assertIn("liquidity", status)
        self.assertIn("invalidation", status)
        self.assertIn("signal_composition", status)
