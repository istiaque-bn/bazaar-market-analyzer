"""Part 2 of the "make Bazaar more futuristic" roadmap: per-stock beta vs.
the exchange index. compute_beta() takes the target stock's own price
frame directly (never persisted here) and reads the exchange-wide index
return series from real PriceHistory rows for a set of peer stocks, so a
perfectly linear synthetic relationship should recover an exact beta."""
import numpy as np
import pandas as pd
from django.test import TestCase

from market.models import Exchange, PriceHistory, Stock
from market.services.close_learn import MIN_BETA_SAMPLE, _clear_context_cache, compute_beta

N_DAYS = 70


def _dates(n=N_DAYS):
    return pd.bdate_range("2026-01-01", periods=n, freq="C", weekmask="Sun Mon Tue Wed Thu")


def _seed_peer_stocks(returns, exchange=Exchange.DSE, n_peers=2):
    """Two peers with the *same* daily return each day, so their
    equal-weight mean (the "index") is exactly `returns`, with no
    cross-stock averaging noise to account for in the assertions."""
    dates = _dates(len(returns))
    for i in range(n_peers):
        stock = Stock.objects.create(exchange=exchange, trading_code=f"PEER{i}", company_name=f"Peer {i}", is_active=True)
        closes = 100 * np.cumprod(1 + np.asarray(returns))
        PriceHistory.objects.bulk_create(
            PriceHistory(
                stock=stock, date=d, open=c, high=c * 1.01, low=c * 0.99, close=c, volume=1000, value=c * 1000
            )
            for d, c in zip(dates, closes)
        )


def _target_df(returns, start_price=100.0):
    dates = _dates(len(returns))
    closes = start_price * np.cumprod(1 + np.asarray(returns))
    return pd.DataFrame({"date": dates, "close": closes})


class ComputeBetaTests(TestCase):
    def setUp(self):
        _clear_context_cache()

    def test_perfectly_linear_relationship_recovers_exact_beta(self):
        rng = np.random.default_rng(0)
        index_returns = rng.normal(0, 0.01, N_DAYS)
        _seed_peer_stocks(index_returns)
        k = 1.8
        target_returns = k * index_returns
        df = _target_df(target_returns)

        beta, pairs = compute_beta(df, exchange=Exchange.DSE, window=90)

        self.assertIsNotNone(beta)
        self.assertAlmostEqual(beta, k, places=2)
        self.assertGreaterEqual(len(pairs), MIN_BETA_SAMPLE)
        self.assertEqual(set(pairs[0].keys()), {"date", "stock_ret", "index_ret"})

    def test_defensive_stock_has_beta_below_one(self):
        rng = np.random.default_rng(1)
        index_returns = rng.normal(0, 0.01, N_DAYS)
        _seed_peer_stocks(index_returns)
        target_returns = 0.4 * index_returns
        df = _target_df(target_returns)

        beta, _ = compute_beta(df, exchange=Exchange.DSE, window=90)
        self.assertIsNotNone(beta)
        self.assertLess(beta, 1.0)

    def test_empty_price_frame_returns_none(self):
        beta, pairs = compute_beta(pd.DataFrame(), exchange=Exchange.DSE)
        self.assertIsNone(beta)
        self.assertEqual(pairs, [])

    def test_no_exchange_context_available_returns_none(self):
        df = _target_df(np.full(N_DAYS, 0.01))
        beta, pairs = compute_beta(df, exchange=Exchange.DSE, window=90)
        self.assertIsNone(beta)
        self.assertEqual(pairs, [])

    def test_too_few_overlapping_rows_returns_none(self):
        rng = np.random.default_rng(2)
        index_returns = rng.normal(0, 0.01, 10)
        _seed_peer_stocks(index_returns)
        df = _target_df(index_returns)

        beta, pairs = compute_beta(df, exchange=Exchange.DSE, window=90)
        self.assertIsNone(beta)
        self.assertEqual(pairs, [])

    def test_zero_variance_index_returns_none_not_a_divide_by_zero_crash(self):
        flat_returns = np.zeros(N_DAYS)
        _seed_peer_stocks(flat_returns)
        df = _target_df(np.full(N_DAYS, 0.005))

        beta, pairs = compute_beta(df, exchange=Exchange.DSE, window=90)
        self.assertIsNone(beta)
        self.assertEqual(pairs, [])
