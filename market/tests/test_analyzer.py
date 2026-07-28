from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from market.models import AnalysisResult, Exchange, PriceHistory, SignalAction, Stock
from market.services.analyzer import analyze_stock
from market.services.predictor import Prediction


class IsSafeBuyStaysConsistentWithBlendedScoreTests(TestCase):
    """is_safe_buy is computed from the pre-blend rule score/confidence
    (score>=28, confidence>=0.4). If ML blending pulls the score down but
    action_from_score(blended) still returns BUY (>=25), is_safe_buy must be
    recomputed against the blended score too, not left stale from the rule
    pass."""

    def setUp(self):
        self.stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="TEST", company_name="Test Co")
        base_date = timezone.localdate() - timedelta(days=90)
        price = 100.0
        history = []
        for i in range(90):
            price += 0.1
            history.append(
                PriceHistory(
                    stock=self.stock,
                    date=base_date + timedelta(days=i),
                    open=price,
                    high=price + 0.5,
                    low=price - 0.5,
                    close=price,
                    volume=10000,
                )
            )
        PriceHistory.objects.bulk_create(history)

    def _canned_prediction(self, score=30.0):
        return Prediction(
            action=SignalAction.BUY,
            score=score,
            confidence=0.5,
            risk_level="medium",
            is_safe_buy=True,
            maturity_days_est=10,
            peak_days_est=5,
            expected_return_pct=5.0,
            probability=0.6,
            rationale="test",
            features={},
            patterns=[],
        )

    def test_is_safe_buy_cleared_when_blend_drops_score_below_threshold(self):
        # blend_score(30, 0.59) = 0.7*30 + 0.3*(0.59-0.5)*100*2 = 26.4:
        # action_from_score(26.4) is still BUY (>=25), but 26.4 < 28, so the
        # rule-derived is_safe_buy=True must not survive the blend.
        with mock.patch("market.services.analyzer.predict_stock", return_value=self._canned_prediction(30.0)), \
             mock.patch("market.services.analyzer.ml_probability", return_value=0.59):
            result = analyze_stock(self.stock, use_ml=True)

        self.assertEqual(result.action, SignalAction.BUY)
        self.assertAlmostEqual(result.score, 26.4, places=1)
        self.assertFalse(result.is_safe_buy)

    def test_is_safe_buy_kept_when_blend_stays_above_threshold(self):
        # blend_score(90, 0.9) stays comfortably >= 28.
        with mock.patch("market.services.analyzer.predict_stock", return_value=self._canned_prediction(90.0)), \
             mock.patch("market.services.analyzer.ml_probability", return_value=0.9):
            result = analyze_stock(self.stock, use_ml=True)

        self.assertEqual(result.action, SignalAction.BUY)
        self.assertGreaterEqual(result.score, 28)
        self.assertTrue(result.is_safe_buy)
