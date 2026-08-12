from datetime import timedelta
from unittest import mock

import numpy as np
import pandas as pd
from django.test import TestCase
from django.utils import timezone

from market.models import AnalysisResult, Exchange, PriceHistory, SignalAction, Stock, TechnicalSnapshot
from market.services.analyzer import analyze_stock
from market.services.close_learn import _clear_context_cache
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


class AnalyzeStockBetaTests(TestCase):
    """analyze_stock() must persist compute_beta()'s result onto the
    stock's TechnicalSnapshot (Part 2 of the futuristic-dashboard roadmap:
    per-stock beta vs. the exchange index)."""

    def setUp(self):
        _clear_context_cache()

    def test_technical_snapshot_gets_a_beta_when_theres_enough_history(self):
        dates = pd.bdate_range(end=timezone.localdate(), periods=70, freq="C", weekmask="Sun Mon Tue Wed Thu")
        rng = np.random.default_rng(42)
        index_returns = rng.normal(0, 0.01, len(dates))

        for i in range(2):
            peer = Stock.objects.create(exchange=Exchange.DSE, trading_code=f"ANLPEER{i}", company_name="Peer", is_active=True)
            closes = 100 * np.cumprod(1 + index_returns)
            PriceHistory.objects.bulk_create(
                PriceHistory(stock=peer, date=d.date(), open=c, high=c * 1.01, low=c * 0.99, close=c, volume=1000, value=c * 1000)
                for d, c in zip(dates, closes)
            )

        stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="ANLTARGET", company_name="Target", is_active=True)
        target_closes = 100 * np.cumprod(1 + 1.4 * index_returns)
        PriceHistory.objects.bulk_create(
            PriceHistory(stock=stock, date=d.date(), open=c, high=c * 1.01, low=c * 0.99, close=c, volume=1000, value=c * 1000)
            for d, c in zip(dates, target_closes)
        )

        with mock.patch("market.services.analyzer.ml_probability", return_value=None):
            analyze_stock(stock, use_ml=True)

        tech = TechnicalSnapshot.objects.get(stock=stock)
        self.assertIsNotNone(tech.beta_90d)
        # Unlike test_beta.py's isolated unit tests (which pass a df
        # directly, never persisting the target to PriceHistory), here
        # analyze_stock() reads the target's own history from the DB, so
        # it also contributes to its own equal-weight index baseline
        # (build_exchange_context has no self-exclusion). With 2 peers at
        # the "clean" 1.0x and the target genuinely 1.4x as volatile, that
        # self-inclusion always pulls the observed beta strictly between
        # the peers' 1.0 and the target's true 1.4 — never outside it.
        self.assertGreater(tech.beta_90d, 1.0)
        self.assertLess(tech.beta_90d, 1.4)

    def test_technical_snapshot_beta_is_none_without_enough_history(self):
        stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="ANLSHORT", company_name="Short", is_active=True)
        base = timezone.localdate() - timedelta(days=5)
        PriceHistory.objects.bulk_create(
            PriceHistory(stock=stock, date=base + timedelta(days=i), open=10, high=11, low=9, close=10 + i * 0.1, volume=100)
            for i in range(5)
        )
        with mock.patch("market.services.analyzer.ml_probability", return_value=None):
            analyze_stock(stock, use_ml=True)

        tech = TechnicalSnapshot.objects.get(stock=stock)
        self.assertIsNone(tech.beta_90d)
