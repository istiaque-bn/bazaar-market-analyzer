"""
Phase 6 — market-data provenance and quality controls.

Covers: OHLCV validation, provenance backfill (legacy rows default to
"unknown", never guessed live), demo/synthetic isolation from live
analysis/training/backtests, revision snapshotting on overwrite,
synthetic-can't-clobber-real protection, quality-scan detection (bad
OHLC, abnormal jumps, stale runs, gaps — never deleting a row), and
import-batch failure recovery. None of these touch the real db.sqlite3.
"""
from datetime import date, timedelta
from unittest import mock

from django.test import SimpleTestCase, TestCase

from market.models import (
    DataSource,
    Exchange,
    ImportBatch,
    PriceHistory,
    PriceHistoryRevision,
    Stock,
)
from market.services import dse_fetcher
from market.services.backtest_engine import CostConfig, PortfolioBacktester, PortfolioConfig
from market.services.data_quality import (
    blocks_synthetic_overwrite,
    provenance_report,
    run_quality_scan,
    scan_stock_quality,
    stock_provenance_summary,
    validate_ohlcv,
)


def _mk_stock(exchange=Exchange.DSE, code="TST"):
    return Stock.objects.create(exchange=exchange, trading_code=code, company_name=code, is_active=True)


def _mk_bar(stock, d, close, **overrides):
    defaults = dict(open=close, high=close, low=close, close=close, volume=1000)
    defaults.update(overrides)
    return PriceHistory.objects.create(stock=stock, date=d, **defaults)


class ValidateOhlcvTests(SimpleTestCase):
    def test_valid_bar_has_no_flags(self):
        self.assertEqual(validate_ohlcv(100, 105, 98, 102, 5000), [])

    def test_non_positive_prices_are_flagged(self):
        flags = validate_ohlcv(0, 10, 5, 8, 100)
        self.assertIn("non_positive_open", flags)

    def test_negative_close_is_flagged(self):
        self.assertIn("non_positive_close", validate_ohlcv(10, 12, 8, -1, 100))

    def test_negative_volume_is_flagged(self):
        self.assertIn("negative_volume", validate_ohlcv(10, 12, 8, 11, -5))

    def test_high_below_low_is_flagged(self):
        self.assertIn("high_below_low", validate_ohlcv(10, 5, 12, 8, 100))

    def test_open_outside_high_low_is_flagged(self):
        self.assertIn("open_out_of_range", validate_ohlcv(50, 20, 10, 15, 100))

    def test_close_outside_high_low_is_flagged(self):
        self.assertIn("close_out_of_range", validate_ohlcv(15, 20, 10, 50, 100))

    def test_none_values_are_flagged_not_raised(self):
        flags = validate_ohlcv(None, None, None, None, None)
        self.assertIn("non_positive_open", flags)
        self.assertIn("non_positive_close", flags)


class ProvenanceBackfillDefaultsTests(TestCase):
    """A row created without explicit provenance (as every pre-Phase-6 row
    was) must be labeled unknown, never guessed as live."""

    def test_row_created_without_explicit_source_defaults_to_unknown(self):
        stock = _mk_stock()
        row = _mk_bar(stock, date(2024, 1, 1), 100.0)
        self.assertEqual(row.source, DataSource.UNKNOWN)
        self.assertFalse(row.is_synthetic)
        self.assertIsNone(row.fetched_at)
        self.assertEqual(row.adjustment_status, "unknown")
        self.assertEqual(row.quality_flags, [])


class LiveQuerysetExcludesSyntheticTests(TestCase):
    def test_live_excludes_synthetic_but_keeps_unknown_and_real(self):
        stock = _mk_stock()
        _mk_bar(stock, date(2024, 1, 1), 100.0, source=DataSource.UNKNOWN, is_synthetic=False)
        _mk_bar(stock, date(2024, 1, 2), 101.0, source=DataSource.DSE_HISTORY, is_synthetic=False)
        _mk_bar(stock, date(2024, 1, 3), 999.0, source=DataSource.SYNTHETIC_DEMO, is_synthetic=True)

        live_dates = set(PriceHistory.objects.live().filter(stock=stock).values_list("date", flat=True))
        self.assertEqual(live_dates, {date(2024, 1, 1), date(2024, 1, 2)})
        self.assertEqual(PriceHistory.objects.demo_only().filter(stock=stock).count(), 1)
        self.assertEqual(PriceHistory.objects.filter(stock=stock).count(), 3)


class SaveHistorySourceTaggingTests(TestCase):
    def _df(self, rows):
        import pandas as pd

        return pd.DataFrame(rows)

    def test_rows_are_tagged_with_the_given_source(self):
        stock = _mk_stock()
        df = self._df(
            [{"date": date(2024, 1, 1), "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000, "value": 0}]
        )
        dse_fetcher.save_history(stock, df, source=DataSource.DSE_HISTORY)
        row = PriceHistory.objects.get(stock=stock, date=date(2024, 1, 1))
        self.assertEqual(row.source, DataSource.DSE_HISTORY)
        self.assertFalse(row.is_synthetic)
        self.assertIsNotNone(row.fetched_at)
        self.assertEqual(row.adjustment_status, "raw")

    def test_synthetic_source_is_marked_is_synthetic(self):
        stock = _mk_stock()
        df = self._df(
            [{"date": date(2024, 1, 1), "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000, "value": 0}]
        )
        dse_fetcher.save_history(stock, df, source=DataSource.SYNTHETIC_DEMO)
        row = PriceHistory.objects.get(stock=stock, date=date(2024, 1, 1))
        self.assertTrue(row.is_synthetic)

    def test_synthetic_write_never_overwrites_a_confirmed_real_row(self):
        stock = _mk_stock()
        _mk_bar(stock, date(2024, 1, 1), 100.0, source=DataSource.DSE_HISTORY)
        df = self._df(
            [{"date": date(2024, 1, 1), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "value": 0}]
        )
        dse_fetcher.save_history(stock, df, source=DataSource.SYNTHETIC_DEMO)
        row = PriceHistory.objects.get(stock=stock, date=date(2024, 1, 1))
        self.assertEqual(row.close, 100.0, "a synthetic write must never clobber confirmed-real data")
        self.assertEqual(row.source, DataSource.DSE_HISTORY)

    def test_overwriting_an_existing_row_with_different_values_writes_a_revision(self):
        stock = _mk_stock()
        _mk_bar(stock, date(2024, 1, 5), 100.0, source=DataSource.DSE_HISTORY, volume=1000)
        df = self._df(
            [{"date": date(2024, 1, 5), "open": 100, "high": 112, "low": 99, "close": 110.0, "volume": 2000, "value": 0}]
        )
        dse_fetcher.save_history(stock, df, source=DataSource.DSE_HISTORY)
        revisions = PriceHistoryRevision.objects.filter(stock=stock, date=date(2024, 1, 5))
        self.assertEqual(revisions.count(), 1)
        rev = revisions.first()
        self.assertEqual(rev.previous_close, 100.0)
        self.assertEqual(rev.previous_volume, 1000)

    def test_rewriting_identical_values_does_not_spawn_a_revision(self):
        stock = _mk_stock()
        _mk_bar(stock, date(2024, 1, 5), 100.0, source=DataSource.DSE_HISTORY, open=100.0, high=100.0, low=100.0, volume=1000)
        df = self._df(
            [{"date": date(2024, 1, 5), "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1000, "value": 0}]
        )
        dse_fetcher.save_history(stock, df, source=DataSource.DSE_HISTORY)
        self.assertEqual(PriceHistoryRevision.objects.filter(stock=stock, date=date(2024, 1, 5)).count(), 0)

    def test_impossible_ohlc_is_flagged_not_rejected(self):
        """Requirement 3/4: validate, don't silently reject — the row is
        still written and still usable, just flagged."""
        stock = _mk_stock()
        df = self._df(
            [{"date": date(2024, 1, 1), "open": 500, "high": 20, "low": 10, "close": 15, "volume": 100, "value": 0}]
        )
        n = dse_fetcher.save_history(stock, df, source=DataSource.DSE_HISTORY)
        self.assertEqual(n, 1)
        row = PriceHistory.objects.get(stock=stock, date=date(2024, 1, 1))
        self.assertIn("open_out_of_range", row.quality_flags)


class UpsertLiveQuotesTaggingTests(TestCase):
    def test_new_live_row_is_tagged_and_existing_row_gets_a_revision(self):
        import pandas as pd

        # 2026-08-02 is a Sunday (a real DSE trading day) and isn't in the
        # seeded holiday list — pinned explicitly so this test doesn't
        # depend on which real weekday it happens to run on (upsert_live_quotes
        # is now a no-op on weekends/holidays, see test_no_row_written_on_a_non_trading_day below).
        today = date(2026, 8, 2)
        stock = _mk_stock()
        df1 = pd.DataFrame([{"trading_code": stock.trading_code, "ltp": 50.0, "high": 51, "low": 49, "volume": 1000, "open": 50.0, "value": 0}])
        with mock.patch("market.services.dse_fetcher.timezone.localdate", return_value=today):
            dse_fetcher.upsert_live_quotes(df1, Exchange.DSE, source=DataSource.DSE_LIVE)
        row = PriceHistory.objects.get(stock=stock, date=today)
        self.assertEqual(row.source, DataSource.DSE_LIVE)
        self.assertIsNotNone(row.fetched_at)

        df2 = pd.DataFrame([{"trading_code": stock.trading_code, "ltp": 55.0, "high": 56, "low": 49, "volume": 2000, "open": 50.0, "value": 0}])
        with mock.patch("market.services.dse_fetcher.timezone.localdate", return_value=today):
            dse_fetcher.upsert_live_quotes(df2, Exchange.DSE, source=DataSource.DSE_LIVE)
        self.assertEqual(PriceHistoryRevision.objects.filter(stock=stock, date=today).count(), 1)
        row.refresh_from_db()
        self.assertEqual(row.close, 55.0)

    def test_no_price_history_row_written_on_a_non_trading_day(self):
        """A weekend/holiday "today" must not get a fabricated PriceHistory
        row — DSE's live-quote endpoint keeps serving the last real close
        with no "market closed" signal, so writing it under today's date
        would silently duplicate a bar for a day that never traded."""
        import pandas as pd

        saturday = date(2026, 8, 8)
        stock = _mk_stock(code="TSTWKND")
        df = pd.DataFrame([{"trading_code": stock.trading_code, "ltp": 50.0, "high": 51, "low": 49, "volume": 1000, "open": 50.0, "value": 0}])
        with mock.patch("market.services.dse_fetcher.timezone.localdate", return_value=saturday):
            dse_fetcher.upsert_live_quotes(df, Exchange.DSE, source=DataSource.DSE_LIVE)

        self.assertFalse(PriceHistory.objects.filter(stock=stock, date=saturday).exists())
        # Stock.last_price should still refresh so the ticker stays current.
        stock.refresh_from_db()
        self.assertEqual(stock.last_price, 50.0)


class QualityScanDetectionTests(TestCase):
    def test_detects_abnormal_jump(self):
        stock = _mk_stock(code="JUMP")
        for i in range(15):
            _mk_bar(stock, date(2024, 1, 1) + timedelta(days=i), 100.0)
        _mk_bar(stock, date(2024, 1, 16), 200.0)  # +100% overnight
        flags_by_id, counts, gaps = scan_stock_quality(stock)
        self.assertGreater(counts.get("abnormal_jump", 0), 0)

    def test_detects_stale_quote_run(self):
        stock = _mk_stock(code="STALE")
        for i in range(10):
            _mk_bar(stock, date(2024, 1, 1) + timedelta(days=i), 100.0, volume=1000)
        flags_by_id, counts, gaps = scan_stock_quality(stock)
        self.assertGreater(counts.get("stale_quote_run", 0), 0)

    def test_zero_volume_run_is_not_flagged_stale(self):
        """A flat close with zero volume is a normal non-trading/suspended
        day, not a stuck feed — must not be flagged stale."""
        stock = _mk_stock(code="ZEROVOL")
        for i in range(10):
            _mk_bar(stock, date(2024, 1, 1) + timedelta(days=i), 100.0, volume=0)
        flags_by_id, counts, gaps = scan_stock_quality(stock)
        self.assertEqual(counts.get("stale_quote_run", 0), 0)

    def test_detects_missing_session_gap(self):
        stock = _mk_stock(code="GAPPY")
        _mk_bar(stock, date(2024, 1, 1), 100.0)
        _mk_bar(stock, date(2024, 3, 1), 101.0)  # ~60 day gap
        flags_by_id, counts, gaps = scan_stock_quality(stock)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["trading_code"], "GAPPY")

    def test_impossible_ohlc_detected(self):
        stock = _mk_stock(code="BADOHLC")
        _mk_bar(stock, date(2024, 1, 1), 100.0, open=500, high=20, low=10)
        flags_by_id, counts, gaps = scan_stock_quality(stock)
        self.assertGreater(counts.get("open_out_of_range", 0), 0)

    def test_scan_never_deletes_rows(self):
        stock = _mk_stock(code="KEEPALL")
        for i in range(20):
            _mk_bar(stock, date(2024, 1, 1) + timedelta(days=i), 100.0, open=-5)  # all flagged bad
        before = PriceHistory.objects.filter(stock=stock).count()
        run_quality_scan()
        after = PriceHistory.objects.filter(stock=stock).count()
        self.assertEqual(before, after)
        self.assertTrue(all(PriceHistory.objects.filter(stock=stock).values_list("quality_flags", flat=True)))

    def test_run_quality_scan_writes_flags_to_db(self):
        stock = _mk_stock(code="WRITEFLAG")
        _mk_bar(stock, date(2024, 1, 1), 100.0, open=-1)
        result = run_quality_scan()
        self.assertTrue(result["ok"])
        row = PriceHistory.objects.get(stock=stock, date=date(2024, 1, 1))
        self.assertIn("non_positive_open", row.quality_flags)

    def test_rescan_clears_flags_that_no_longer_apply(self):
        stock = _mk_stock(code="CLEARED")
        row = _mk_bar(stock, date(2024, 1, 1), 100.0, open=-1)
        run_quality_scan()
        row.refresh_from_db()
        self.assertTrue(row.quality_flags)
        row.open = 100.0
        row.save()
        run_quality_scan()
        row.refresh_from_db()
        self.assertEqual(row.quality_flags, [])


class BlocksSyntheticOverwriteTests(SimpleTestCase):
    def test_none_existing_never_blocks(self):
        self.assertFalse(blocks_synthetic_overwrite(None, DataSource.SYNTHETIC_DEMO))

    def test_real_source_write_never_blocked(self):
        existing = PriceHistory(source=DataSource.UNKNOWN)
        self.assertFalse(blocks_synthetic_overwrite(existing, DataSource.DSE_HISTORY))


class DemoIsolationBacktestTests(TestCase):
    def _price_series(self, stock, n=120, source=DataSource.DSE_HISTORY, is_synthetic=False):
        import numpy as np

        rng = np.random.default_rng(3)
        closes = 100 + np.cumsum(rng.normal(0, 1, n))
        rows = [
            PriceHistory(
                stock=stock,
                date=date(2024, 1, 1) + timedelta(days=i),
                open=float(c) - 0.2,
                high=float(c) + 0.5,
                low=float(c) - 0.5,
                close=float(c),
                volume=5000,
                source=source,
                is_synthetic=is_synthetic,
            )
            for i, c in enumerate(closes)
        ]
        PriceHistory.objects.bulk_create(rows)

    def test_backtest_excludes_synthetic_only_stock_by_default(self):
        stock = _mk_stock(code="DEMOONLY")
        self._price_series(stock, source=DataSource.SYNTHETIC_DEMO, is_synthetic=True)
        start, end = date(2024, 1, 1), date(2024, 1, 1) + timedelta(days=119)
        engine = PortfolioBacktester(Exchange.DSE, start, end, CostConfig(), PortfolioConfig(), include_demo=False)
        result = engine.run()
        self.assertFalse(result["ok"], "a purely-synthetic universe must not be tradeable by default")

    def test_backtest_includes_synthetic_only_stock_when_include_demo_true(self):
        stock = _mk_stock(code="DEMOONLY2")
        self._price_series(stock, source=DataSource.SYNTHETIC_DEMO, is_synthetic=True)
        start, end = date(2024, 1, 1), date(2024, 1, 1) + timedelta(days=119)
        engine = PortfolioBacktester(Exchange.DSE, start, end, CostConfig(), PortfolioConfig(), include_demo=True)
        result = engine.run()
        self.assertTrue(result["ok"])

    def test_real_data_stock_is_included_by_default(self):
        stock = _mk_stock(code="REALDATA")
        self._price_series(stock, source=DataSource.DSE_HISTORY, is_synthetic=False)
        start, end = date(2024, 1, 1), date(2024, 1, 1) + timedelta(days=119)
        engine = PortfolioBacktester(Exchange.DSE, start, end, CostConfig(), PortfolioConfig(), include_demo=False)
        result = engine.run()
        self.assertTrue(result["ok"])


class ProvenanceReportTests(TestCase):
    def test_stock_summary_reports_latest_source_and_raw_adjustment_state(self):
        stock = _mk_stock()
        _mk_bar(stock, date(2024, 1, 2), 101.0, source=DataSource.DSE_HISTORY, adjustment_status="raw")

        summary = stock_provenance_summary(stock)

        self.assertTrue(summary["available"])
        self.assertEqual(summary["latest_source"], "DSE historical archive/bdshare")
        self.assertEqual(summary["latest_adjustment"], "Raw / as-received (no corporate-action adjustment applied)")
        self.assertEqual(summary["source_counts"][DataSource.DSE_HISTORY], 1)

    def test_counts_reflect_synthetic_and_unknown_rows(self):
        stock = _mk_stock()
        _mk_bar(stock, date(2024, 1, 1), 100.0)  # unknown
        _mk_bar(stock, date(2024, 1, 2), 101.0, source=DataSource.DSE_HISTORY)
        _mk_bar(stock, date(2024, 1, 3), 102.0, source=DataSource.SYNTHETIC_DEMO, is_synthetic=True)

        report = provenance_report()
        self.assertEqual(report["total_rows"], 3)
        self.assertEqual(report["synthetic_rows"], 1)
        self.assertEqual(report["unknown_provenance_rows"], 1)
        self.assertIn(DataSource.UNKNOWN, report["by_source"])
        self.assertIn(DataSource.DSE_HISTORY, report["by_source"])

    def test_data_quality_page_renders_for_staff(self):
        from django.contrib.auth.models import User
        from django.test import Client

        User.objects.create_superuser("qa_staff", "qa@example.com", "pw12345!")
        client = Client()
        client.login(username="qa_staff", password="pw12345!")
        resp = client.get("/data-quality/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Data quality")

    def test_data_quality_page_forbidden_for_non_staff(self):
        from django.contrib.auth.models import User
        from django.test import Client

        User.objects.create_user("plain_user", "u@example.com", "pw12345!")
        client = Client()
        client.login(username="plain_user", password="pw12345!")
        resp = client.get("/data-quality/")
        self.assertIn(resp.status_code, (302, 403))


class ImportBatchFailureRecoveryTests(TestCase):
    def test_sync_dse_history_marks_batch_error_and_finishes_on_exception(self):
        stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="BOOM", is_active=True)

        def _boom_fetch(code, start, end):
            import pandas as pd

            return pd.DataFrame([{"date": date(2024, 1, 1), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "value": 0}]), "test"

        def _boom_save(*a, **k):
            raise RuntimeError("simulated write failure")

        with mock.patch.object(dse_fetcher, "fetch_dse_history", side_effect=_boom_fetch), mock.patch.object(
            dse_fetcher, "save_history", side_effect=_boom_save
        ):
            with self.assertRaises(RuntimeError):
                dse_fetcher.sync_dse_history(codes=["BOOM"])

        batch = ImportBatch.objects.filter(source=DataSource.DSE_HISTORY, exchange=Exchange.DSE).order_by("-started_at").first()
        self.assertIsNotNone(batch)
        self.assertIsNotNone(batch.finished_at, "a batch must never be left stuck 'in progress' after a crash")
        self.assertIn("simulated write failure", batch.error)

    def test_successful_batch_has_no_error_and_is_finished(self):
        stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="GOODCODE", is_active=True)

        def _good_fetch(code, start, end):
            import pandas as pd

            return pd.DataFrame([{"date": date(2024, 1, 1), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "value": 0}]), "test"

        with mock.patch.object(dse_fetcher, "fetch_dse_history", side_effect=_good_fetch):
            dse_fetcher.sync_dse_history(codes=["GOODCODE"])

        batch = ImportBatch.objects.filter(source=DataSource.DSE_HISTORY, exchange=Exchange.DSE).order_by("-started_at").first()
        self.assertIsNotNone(batch)
        self.assertIsNotNone(batch.finished_at)
        self.assertEqual(batch.error, "")
        self.assertEqual(batch.row_count, 1)
