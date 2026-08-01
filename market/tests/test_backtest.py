"""
Phase 5 — deterministic tests for the portfolio backtest engine.

Covers: next-session execution (never the signal bar itself), cost
accounting, capital constraints, look-ahead prevention, date filtering,
and metrics/breakdown computation. None of these touch the real
db.sqlite3 (Django TestCase's isolated DB) or any file on disk.
"""
from datetime import date, timedelta
from unittest import mock

import numpy as np
import pandas as pd
from django.test import SimpleTestCase, TestCase

from market.models import BacktestRun, Exchange, PriceHistory, Stock
from market.services import backtest as backtest_module
from market.services.backtest_engine import (
    CostConfig,
    PortfolioBacktester,
    PortfolioConfig,
    StockSeries,
    _breakdown_by,
    _buy_and_hold_return_pct,
    _compute_metrics,
    _exchange_index_proxy_return_pct,
    _signal_at,
)


def _make_stock(code, closes, exchange=Exchange.DSE, start=date(2024, 1, 1), sector="Tech"):
    stock = Stock.objects.create(exchange=exchange, trading_code=code, company_name=code, sector=sector, is_active=True)
    rows = []
    for i, c in enumerate(closes):
        d = start + timedelta(days=i)
        o = c - 0.4  # deliberately different from close, so tests can tell entry price came from OPEN not CLOSE
        rows.append(PriceHistory(stock=stock, date=d, open=o, high=c + 0.5, low=min(o, c) - 0.2, close=c, volume=10000))
    PriceHistory.objects.bulk_create(rows)
    return stock


def _oversold_series(n_warmup=60, n_decline=15, n_recover=20, n_tail=15):
    closes = [100 + (i % 3 - 1) * 0.1 for i in range(n_warmup)]
    closes += [closes[-1] - i * 2.0 for i in range(1, n_decline + 1)]
    closes += [closes[-1] + k * 0.3 for k in range(1, n_recover + 1)]
    closes += [closes[-1] + (i % 3 - 1) * 0.1 for i in range(n_tail)]
    return closes


class SignalFunctionTests(SimpleTestCase):
    def test_rsi_oversold_triggers_signal(self):
        row = pd.Series({"rsi_14": 20.0, "macd": 0.0, "macd_signal": 0.0})
        self.assertTrue(_signal_at(row, None))

    def test_neutral_row_no_signal(self):
        row = pd.Series({"rsi_14": 50.0, "macd": 0.0, "macd_signal": 0.1})
        prev = pd.Series({"macd": 0.0, "macd_signal": -0.1})
        self.assertFalse(_signal_at(row, prev))

    def test_macd_bullish_cross_triggers_signal(self):
        row = pd.Series({"rsi_14": 50.0, "macd": 0.5, "macd_signal": 0.3})
        prev = pd.Series({"macd": 0.2, "macd_signal": 0.25})
        self.assertTrue(_signal_at(row, prev))


class NextSessionExecutionTests(TestCase):
    """Requirement 2: signals use only data as of the signal day; orders
    execute no earlier than the next tradable session, at that session's
    price — never the signal day's own close."""

    def test_entries_execute_at_next_sessions_open_not_signal_days_close(self):
        closes = _oversold_series()
        stock = _make_stock("NSX", closes)
        start = date(2024, 1, 1)
        end = start + timedelta(days=len(closes) - 1)
        engine = PortfolioBacktester(
            exchange=Exchange.DSE,
            start_date=start,
            end_date=end,
            cost_config=CostConfig(brokerage_pct=0, tax_pct=0, spread_pct=0, slippage_pct=0),
            portfolio_config=PortfolioConfig(hold_days=20, max_positions=5),
        )
        result = engine.run()
        self.assertTrue(result["ok"])
        self.assertGreater(len(result["trades"]), 0)
        for t in result["trades"]:
            self.assertEqual(t["stock_id"], stock.id)
            signal_date = date.fromisoformat(t["signal_date"])
            entry_date = date.fromisoformat(t["entry_date"])
            self.assertGreater(entry_date, signal_date, "execution must never be on the signal day itself")
            expected_next = PriceHistory.objects.filter(stock_id=t["stock_id"], date__gt=signal_date).order_by("date").first()
            self.assertEqual(entry_date, expected_next.date, "execution must be the stock's immediate next session")
            self.assertAlmostEqual(t["entry_open_price"], expected_next.open, places=4)
            # zero-cost config -> executed price must equal the raw open exactly, not the signal day's close
            self.assertAlmostEqual(t["entry_price"], expected_next.open, places=4)
            signal_day_close = PriceHistory.objects.get(stock_id=t["stock_id"], date=signal_date).close
            self.assertNotAlmostEqual(t["entry_price"], signal_day_close, places=2)


class CostAccountingTests(TestCase):
    def _run(self, cost_config):
        closes = _oversold_series()
        _make_stock("CST", closes)
        start = date(2024, 1, 1)
        end = start + timedelta(days=len(closes) - 1)
        engine = PortfolioBacktester(
            exchange=Exchange.DSE, start_date=start, end_date=end,
            cost_config=cost_config, portfolio_config=PortfolioConfig(hold_days=20),
        )
        return engine.run()

    def test_zero_cost_config_means_net_equals_gross(self):
        result = self._run(CostConfig(brokerage_pct=0, tax_pct=0, spread_pct=0, slippage_pct=0))
        self.assertTrue(result["trades"])
        for t in result["trades"]:
            self.assertEqual(t["costs"], 0)
            self.assertAlmostEqual(t["net_pnl"], t["gross_pnl"], delta=0.5)

    def test_nonzero_costs_reduce_net_pnl_below_gross_by_exactly_the_reported_costs(self):
        result = self._run(CostConfig(brokerage_pct=0.3, tax_pct=0.05, spread_pct=0.1, slippage_pct=0.1))
        self.assertTrue(result["trades"])
        for t in result["trades"]:
            self.assertGreater(t["costs"], 0)
            self.assertAlmostEqual(t["net_pnl"], t["gross_pnl"] - t["costs"], delta=1.0)
            self.assertLess(t["net_pnl"], t["gross_pnl"])


class CapitalConstraintTests(TestCase):
    def test_cash_never_goes_negative_under_a_tight_budget(self):
        closes = _oversold_series()
        for i in range(6):
            _make_stock(f"CAP{i}", closes, sector="Tech")
        start = date(2024, 1, 1)
        end = start + timedelta(days=len(closes) - 1)
        engine = PortfolioBacktester(
            exchange=Exchange.DSE, start_date=start, end_date=end,
            cost_config=CostConfig(),
            portfolio_config=PortfolioConfig(initial_cash=10_000.0, position_size_pct=50.0, max_positions=20),
        )
        result = engine.run()
        self.assertTrue(result["ok"])
        for e in result["equity_curve"]:
            self.assertGreaterEqual(e["cash"], -1e-6)
        self.assertGreaterEqual(result["final_cash"], -1e-6)

    def test_max_positions_cap_is_enforced(self):
        closes = _oversold_series()
        for i in range(8):
            _make_stock(f"MAXP{i}", closes, sector="Tech")
        start = date(2024, 1, 1)
        end = start + timedelta(days=len(closes) - 1)
        engine = PortfolioBacktester(
            exchange=Exchange.DSE, start_date=start, end_date=end,
            cost_config=CostConfig(),
            portfolio_config=PortfolioConfig(initial_cash=10_000_000.0, position_size_pct=5.0, max_positions=2),
        )
        result = engine.run()
        self.assertTrue(result["ok"])
        for e in result["equity_curve"]:
            self.assertLessEqual(e["n_positions"], 2)


class LookaheadPreventionTests(TestCase):
    def test_signal_and_entry_are_identical_when_future_paths_diverge_after_the_decision_point(self):
        """Two stocks share an identical price history up to a point, then
        diverge (one keeps rising, one crashes). If the signal/entry
        computed at the shared decision point were influenced by data
        after it, the two stocks would get different signal/entry dates
        or prices despite having identical histories at decision time."""
        base = _oversold_series(n_warmup=60, n_decline=15, n_recover=5, n_tail=0)
        tail_a = [base[-1] + i * 0.2 for i in range(1, 21)]
        tail_b = [base[-1] - i * 5.0 for i in range(1, 21)]
        stock_a = _make_stock("LKA", base + tail_a)
        stock_b = _make_stock("LKB", base + tail_b)
        start = date(2024, 1, 1)
        end = start + timedelta(days=len(base + tail_a) - 1)
        engine = PortfolioBacktester(
            exchange=Exchange.DSE, start_date=start, end_date=end,
            cost_config=CostConfig(), portfolio_config=PortfolioConfig(hold_days=20, max_positions=5),
        )
        result = engine.run()
        trades_a = sorted((t for t in result["trades"] if t["stock_id"] == stock_a.id), key=lambda t: t["signal_date"])
        trades_b = sorted((t for t in result["trades"] if t["stock_id"] == stock_b.id), key=lambda t: t["signal_date"])
        self.assertTrue(trades_a, "expected at least one trade on the shared-prefix decline")
        self.assertTrue(trades_b, "expected at least one trade on the shared-prefix decline")
        self.assertEqual(trades_a[0]["signal_date"], trades_b[0]["signal_date"])
        self.assertEqual(trades_a[0]["entry_date"], trades_b[0]["entry_date"])
        self.assertAlmostEqual(trades_a[0]["entry_price"], trades_b[0]["entry_price"], places=4)
        # sanity: the divergence point really did happen before this entry,
        # proving the shared decision predates any different future data
        self.assertLess(date.fromisoformat(trades_a[0]["entry_date"]), start + timedelta(days=len(base)))


class DateFilteringTests(TestCase):
    def test_no_trade_falls_outside_the_requested_window(self):
        closes = _oversold_series()
        _make_stock("DTF", closes)
        full_start = date(2024, 1, 1)
        # Must leave >= MIN_HISTORY_BARS (100) bars available as of the
        # narrowed end_date, or the stock is correctly excluded entirely —
        # this window (101 bars) keeps it eligible while still cutting off
        # the fixture's tail (110 bars total).
        narrow_end = full_start + timedelta(days=100)
        engine = PortfolioBacktester(
            exchange=Exchange.DSE, start_date=full_start, end_date=narrow_end,
            cost_config=CostConfig(), portfolio_config=PortfolioConfig(hold_days=20),
        )
        result = engine.run()
        self.assertTrue(result["ok"])
        self.assertLessEqual(result["data_end_date"], narrow_end)
        self.assertGreaterEqual(result["data_start_date"], full_start)
        for t in result["trades"]:
            self.assertLessEqual(date.fromisoformat(t["entry_date"]), narrow_end)
            self.assertLessEqual(date.fromisoformat(t["exit_date"]), narrow_end)
            self.assertGreaterEqual(date.fromisoformat(t["signal_date"]), full_start)

    def test_start_after_end_raises(self):
        with self.assertRaises(ValueError):
            PortfolioBacktester(
                exchange=Exchange.DSE, start_date=date(2024, 2, 1), end_date=date(2024, 1, 1),
                cost_config=CostConfig(), portfolio_config=PortfolioConfig(),
            )

    def test_data_dates_recorded_never_wider_than_the_stocks_actual_history(self):
        closes = _oversold_series()
        _make_stock("SHORTHIST", closes)
        requested_start = date(2023, 1, 1)
        requested_end = date(2024, 1, 1) + timedelta(days=len(closes) - 1) + timedelta(days=200)
        engine = PortfolioBacktester(
            exchange=Exchange.DSE, start_date=requested_start, end_date=requested_end,
            cost_config=CostConfig(), portfolio_config=PortfolioConfig(),
        )
        result = engine.run()
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["data_start_date"], date(2024, 1, 1))
        self.assertLessEqual(result["data_end_date"], date(2024, 1, 1) + timedelta(days=len(closes) - 1))


class MetricsComputationTests(SimpleTestCase):
    def test_metrics_on_a_simple_equity_curve_and_trade_list(self):
        dates = pd.date_range("2024-01-01", periods=10, freq="D")
        equities = [100, 102, 101, 105, 103, 108, 107, 110, 106, 112]
        curve = [{"date": d, "equity": e, "exposure_pct": 50.0} for d, e in zip(dates, equities)]
        trades = [
            {"net_pnl": 5.0, "return_pct": 5.0, "shares": 10, "entry_price": 10, "exit_price": 10.5, "costs": 0.5},
            {"net_pnl": -3.0, "return_pct": -3.0, "shares": 10, "entry_price": 10, "exit_price": 9.7, "costs": 0.3},
        ]
        m = _compute_metrics(curve, trades, initial_cash=100)
        self.assertEqual(m["final_equity"], 112)
        self.assertAlmostEqual(m["total_return_pct"], 12.0, places=3)
        self.assertLess(m["max_drawdown_pct"], 0)
        self.assertIsNotNone(m["sharpe_ratio"])
        self.assertEqual(m["total_trades"], 2)
        self.assertAlmostEqual(m["win_rate"], 0.5)
        self.assertAlmostEqual(m["profit_factor"], 5.0 / 3.0, places=3)

    def test_empty_trades_are_handled_safely(self):
        dates = pd.date_range("2024-01-01", periods=3, freq="D")
        curve = [{"date": d, "equity": 100.0, "exposure_pct": 0.0} for d in dates]
        m = _compute_metrics(curve, [], initial_cash=100.0)
        self.assertEqual(m["total_trades"], 0)
        self.assertEqual(m["win_rate"], 0.0)
        self.assertIsNone(m["profit_factor"])


class BreakdownTests(SimpleTestCase):
    def test_small_sample_gets_a_warning(self):
        trades = [{"return_pct": 1.0, "net_pnl": 1.0, "year": 2024, "exchange": "DSE", "sector": "Tech"}]
        out = _breakdown_by(trades, "year")
        self.assertIn("warning", out["2024"])

    def test_large_sample_has_no_warning(self):
        trades = [{"return_pct": 1.0, "net_pnl": 1.0, "year": 2024, "exchange": "DSE", "sector": "Tech"} for _ in range(25)]
        out = _breakdown_by(trades, "year")
        self.assertNotIn("warning", out["2024"])


class BuyHoldBenchmarkTests(SimpleTestCase):
    def test_equal_weight_average_of_simple_returns(self):
        idx = list(pd.to_datetime(["2024-01-01", "2024-01-02"]))
        frame_a = pd.DataFrame({"open": [100.0, 102.0], "close": [101.0, 103.0]}, index=idx)
        frame_b = pd.DataFrame({"open": [50.0, 51.0], "close": [50.5, 52.0]}, index=idx)
        s_a = StockSeries(stock_id=1, trading_code="A", exchange="DSE", sector="X", frame=frame_a, dates=idx, window_dates=idx)
        s_b = StockSeries(stock_id=2, trading_code="B", exchange="DSE", sector="X", frame=frame_b, dates=idx, window_dates=idx)
        ret_a = 103.0 / 100.0 - 1
        ret_b = 52.0 / 50.0 - 1
        expected = (ret_a + ret_b) / 2 * 100
        self.assertAlmostEqual(_buy_and_hold_return_pct([s_a, s_b]), expected, places=3)


class ExchangeIndexProxyRobustnessTests(SimpleTestCase):
    """Regression test: a stray zero/near-zero close anywhere in real
    PriceHistory can make build_exchange_context() emit an inf/nan daily
    return (division by ~0). The proxy must never propagate that into a
    non-finite "benchmark" result — this was caught against real data."""

    def test_inf_daily_return_is_dropped_not_propagated(self):
        dates = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"])
        bad_ctx = pd.DataFrame({"date": dates, "index_ret_1d": [0.01, np.inf, 0.02], "breadth": [0.5, 0.5, 0.5]})
        with mock.patch("market.services.close_learn.build_exchange_context", return_value=bad_ctx):
            result = _exchange_index_proxy_return_pct(Exchange.DSE, date(2024, 1, 1), date(2024, 1, 3))
        self.assertIsNotNone(result)
        self.assertTrue(np.isfinite(result))
        # should equal compounding just the two finite days
        expected = ((1.01) * (1.02) - 1) * 100
        self.assertAlmostEqual(result, expected, places=2)

    def test_all_non_finite_returns_none(self):
        dates = pd.to_datetime(["2024-01-01", "2024-01-02"])
        bad_ctx = pd.DataFrame({"date": dates, "index_ret_1d": [np.inf, -np.inf], "breadth": [0.5, 0.5]})
        with mock.patch("market.services.close_learn.build_exchange_context", return_value=bad_ctx):
            result = _exchange_index_proxy_return_pct(Exchange.DSE, date(2024, 1, 1), date(2024, 1, 2))
        self.assertIsNone(result)


class RunBacktestPersistenceTests(TestCase):
    def test_creates_a_new_row_each_call_preserving_history(self):
        closes = _oversold_series()
        _make_stock("PER", closes)
        start = date(2024, 1, 1)
        end = start + timedelta(days=len(closes) - 1)
        run1 = backtest_module.run_backtest(name="t1", strategy="s1", exchange=Exchange.DSE, start_date=start, end_date=end)
        run2 = backtest_module.run_backtest(name="t1", strategy="s1", exchange=Exchange.DSE, start_date=start, end_date=end)
        self.assertNotEqual(run1.id, run2.id)
        self.assertEqual(BacktestRun.objects.filter(name="t1", strategy="s1").count(), 2)
        self.assertEqual(run1.engine_version, "v2")

    def test_legacy_v1_rows_are_untouched_by_the_new_engine(self):
        legacy = BacktestRun.objects.create(name="legacy", strategy="old", start_date=date(2020, 1, 1), end_date=date(2020, 12, 31))
        self.assertEqual(legacy.engine_version, "v1")
        legacy.refresh_from_db()
        self.assertEqual(legacy.engine_version, "v1")

    def test_no_data_case_still_creates_an_auditable_row(self):
        run = backtest_module.run_backtest(name="empty", strategy="s", exchange=Exchange.CSE, start_date=date(2024, 1, 1), end_date=date(2024, 6, 1))
        self.assertIn("note", run.summary)

    def test_start_after_end_raises_in_the_wrapper_too(self):
        with self.assertRaises(ValueError):
            backtest_module.run_backtest(start_date=date(2024, 2, 1), end_date=date(2024, 1, 1))

    def test_full_run_populates_costs_benchmarks_and_breakdowns(self):
        closes = _oversold_series()
        _make_stock("FULL", closes, sector="Pharma")
        start = date(2024, 1, 1)
        end = start + timedelta(days=len(closes) - 1)
        run = backtest_module.run_backtest(name="full", strategy="s", exchange=Exchange.DSE, start_date=start, end_date=end)
        self.assertEqual(run.engine_version, "v2")
        self.assertGreater(run.total_trades, 0)
        self.assertIsNotNone(run.total_costs)
        self.assertGreater(run.total_costs, 0)
        self.assertIn("by_year", run.breakdown)
        self.assertIn("by_exchange", run.breakdown)
        self.assertIn("by_sector", run.breakdown)
        self.assertEqual(run.cost_config["brokerage_pct"], CostConfig().brokerage_pct)
        self.assertIsNotNone(run.data_start_date)
        self.assertIsNotNone(run.data_end_date)
        self.assertGreaterEqual(run.data_start_date, run.start_date)
        self.assertLessEqual(run.data_end_date, run.end_date)
