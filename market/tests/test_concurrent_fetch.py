"""market.services.concurrent_fetch — bounded concurrent DSE history fetch.

No live network access — either mocks the top-level fetch_dse_history
(orchestration/concurrency/retry focus, matching test_data_quality.py's
existing mock.patch.object(dse_fetcher, "fetch_dse_history", ...) pattern)
or mocks dse_fetcher._get directly for the rate-limit tests that need real
HTTP-status visibility (matching test_fetcher_parsing.py's `_resp` pattern).
"""
import time
from datetime import date
from unittest import mock

import pandas as pd
from django.test import SimpleTestCase, TestCase

from market.services import concurrent_fetch

START = date(2024, 1, 1)
END = date(2026, 1, 1)


def _df(rows: int = 1) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": [date(2026, 1, i + 1) for i in range(rows)],
            "open": [10.0] * rows,
            "high": [11.0] * rows,
            "low": [9.0] * rows,
            "close": [10.5] * rows,
            "volume": [1000] * rows,
            "value": [100.0] * rows,
        }
    )


def _resp(status_code: int = 200, text: str = "") -> mock.Mock:
    resp = mock.Mock()
    resp.status_code = status_code
    resp.text = text
    resp.raise_for_status = mock.Mock()
    return resp


class PrefetchModesTests(SimpleTestCase):
    """TEST1: normal N-symbol fetch, all succeed — across every mode."""

    def test_threadpool_mode_all_succeed(self):
        codes = [f"SYM{i}" for i in range(10)]
        with mock.patch("market.services.concurrent_fetch.fetch_dse_history", side_effect=lambda c, s, e, **kw: (_df(), "archive")):
            prefetched, stats = concurrent_fetch.prefetch_dse_history(codes, START, END, mode="threadpool", concurrency=5, timeout=5, max_retries=0)
        self.assertEqual(stats["mode"], "threadpool")
        self.assertEqual(stats["attempted"], 10)
        self.assertEqual(stats["successful"], 10)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(len(prefetched), 10)
        self.assertEqual({c for c, _, _ in prefetched}, set(codes))

    def test_asyncio_mode_all_succeed(self):
        codes = [f"SYM{i}" for i in range(10)]
        with mock.patch("market.services.concurrent_fetch.fetch_dse_history", side_effect=lambda c, s, e, **kw: (_df(), "archive")):
            prefetched, stats = concurrent_fetch.prefetch_dse_history(codes, START, END, mode="asyncio", concurrency=5, timeout=5, max_retries=0)
        self.assertEqual(stats["mode"], "asyncio")
        self.assertEqual(stats["successful"], 10)
        self.assertEqual(len(prefetched), 10)

    def test_sequential_mode_all_succeed(self):
        codes = [f"SYM{i}" for i in range(5)]
        with mock.patch("market.services.concurrent_fetch.fetch_dse_history", side_effect=lambda c, s, e, **kw: (_df(), "archive")):
            prefetched, stats = concurrent_fetch.prefetch_dse_history(codes, START, END, mode="sequential", timeout=5, max_retries=0)
        self.assertEqual(stats["mode"], "sequential")
        self.assertEqual(stats["concurrency"], 1)
        self.assertEqual(stats["successful"], 5)

    def test_empty_codes_list_returns_empty_without_error(self):
        prefetched, stats = concurrent_fetch.prefetch_dse_history([], START, END, mode="threadpool", concurrency=5)
        self.assertEqual(prefetched, [])
        self.assertEqual(stats["attempted"], 0)


class SlowAndTimeoutTests(SimpleTestCase):
    def test_one_slow_response_still_succeeds_within_timeout(self):
        """TEST2: one slow provider response must not fail as long as it
        completes inside the allotted timeout, and must not delay the
        other symbols (they run concurrently, not behind it)."""

        def _maybe_slow(code, s, e, **kw):
            if code == "SLOW":
                time.sleep(0.3)
            return _df(), "archive"

        codes = ["A", "SLOW", "B"]
        with mock.patch("market.services.concurrent_fetch.fetch_dse_history", side_effect=_maybe_slow):
            started = time.monotonic()
            prefetched, stats = concurrent_fetch.prefetch_dse_history(codes, START, END, mode="threadpool", concurrency=3, timeout=5, max_retries=0)
            elapsed = time.monotonic() - started
        self.assertEqual(stats["successful"], 3)
        # Concurrent, not serial: total time should be close to the one
        # slow call (~0.3s), not 3x that.
        self.assertLess(elapsed, 1.0)

    def test_one_symbol_timeout_does_not_cancel_others(self):
        """TEST3: a hanging symbol times out on its own; the rest of the
        batch is unaffected."""

        def _maybe_hang(code, s, e, **kw):
            if code == "HANGS":
                time.sleep(2.5)
            return _df(), "archive"

        codes = ["A", "HANGS", "B"]
        with mock.patch("market.services.concurrent_fetch.fetch_dse_history", side_effect=_maybe_hang):
            prefetched, stats = concurrent_fetch.prefetch_dse_history(codes, START, END, mode="threadpool", concurrency=3, timeout=1, max_retries=0)
        self.assertEqual(stats["successful"], 2)
        self.assertEqual(stats["timed_out"], 1)
        by_code = {c: (df, src) for c, df, src in prefetched}
        self.assertIsNotNone(by_code["A"][0])
        self.assertIsNotNone(by_code["B"][0])
        self.assertIsNone(by_code["HANGS"][0])

    def test_one_symbol_timeout_does_not_cancel_others_asyncio(self):
        def _maybe_hang(code, s, e, **kw):
            if code == "HANGS":
                time.sleep(2.5)
            return _df(), "archive"

        codes = ["A", "HANGS", "B"]
        with mock.patch("market.services.concurrent_fetch.fetch_dse_history", side_effect=_maybe_hang):
            prefetched, stats = concurrent_fetch.prefetch_dse_history(codes, START, END, mode="asyncio", concurrency=3, timeout=1, max_retries=0)
        self.assertEqual(stats["successful"], 2)
        self.assertEqual(stats["timed_out"], 1)


class FailureCountingTests(SimpleTestCase):
    def test_multiple_failures_counted_correctly(self):
        """TEST4: several simultaneous failures are each counted, and don't
        affect symbols that succeed."""

        def _mixed(code, s, e, **kw):
            if code in ("BAD1", "BAD2"):
                return None, "none"
            return _df(), "archive"

        codes = ["GOOD1", "BAD1", "GOOD2", "BAD2"]
        with mock.patch("market.services.concurrent_fetch.fetch_dse_history", side_effect=_mixed):
            prefetched, stats = concurrent_fetch.prefetch_dse_history(codes, START, END, mode="threadpool", concurrency=4, timeout=5, max_retries=0)
        self.assertEqual(stats["successful"], 2)
        self.assertEqual(stats["failed"], 2)
        self.assertEqual(stats["attempted"], 4)

    def test_retries_are_counted(self):
        """A symbol that fails once then succeeds on retry is reported
        successful with retry_count reflected in the aggregate."""
        attempts = {"count": 0}

        def _flaky(code, s, e, **kw):
            attempts["count"] += 1
            if attempts["count"] == 1:
                return None, "none"
            return _df(), "archive"

        with mock.patch("market.services.concurrent_fetch.fetch_dse_history", side_effect=_flaky):
            prefetched, stats = concurrent_fetch.prefetch_dse_history(["A"], START, END, mode="threadpool", concurrency=1, timeout=5, max_retries=2)
        self.assertEqual(stats["successful"], 1)
        self.assertGreaterEqual(stats["retried"], 1)


class RateLimitAndOutageTests(SimpleTestCase):
    def test_rate_limit_response_is_detected(self):
        """TEST5: a 429 from the provider is observed and flagged."""
        codes = ["A", "B", "C"]
        with mock.patch("market.services.dse_fetcher.fetch_dse_history_via_bdshare", return_value=None), \
                mock.patch("market.services.dse_fetcher._get", return_value=_resp(status_code=429)):
            prefetched, stats = concurrent_fetch.prefetch_dse_history(codes, START, END, mode="threadpool", concurrency=3, timeout=5, max_retries=0)
        self.assertEqual(stats["rate_limited_symbols"], 3)
        self.assertEqual(stats["failed"], 3)

    def test_total_provider_outage_all_fail_cleanly(self):
        """TEST6: every symbol fails (provider down); the batch completes
        with a clean all-failed result instead of raising."""
        codes = ["A", "B", "C"]
        with mock.patch("market.services.dse_fetcher.fetch_dse_history_via_bdshare", return_value=None), \
                mock.patch("market.services.dse_fetcher._get", return_value=_resp(status_code=503)):
            prefetched, stats = concurrent_fetch.prefetch_dse_history(codes, START, END, mode="threadpool", concurrency=3, timeout=5, max_retries=0)
        self.assertEqual(stats["successful"], 0)
        self.assertEqual(stats["failed"], 3)
        self.assertEqual(len(prefetched), 3)
        for _, df, _ in prefetched:
            self.assertIsNone(df)


class StabilityTests(SimpleTestCase):
    def test_repeated_runs_produce_stable_results(self):
        """TEST10: the same batch run several times over produces the same
        outcome each time (no flaky shared state across runs)."""
        codes = [f"SYM{i}" for i in range(8)]
        results = []
        with mock.patch("market.services.concurrent_fetch.fetch_dse_history", side_effect=lambda c, s, e, **kw: (_df(), "archive")):
            for _ in range(3):
                _, stats = concurrent_fetch.prefetch_dse_history(codes, START, END, mode="threadpool", concurrency=4, timeout=5, max_retries=0)
                results.append((stats["successful"], stats["failed"], stats["timed_out"]))
        self.assertTrue(all(r == results[0] for r in results), results)


class SyncDseHistoryFetchStatsTests(TestCase):
    """sync_dse_history's return dict gains fetch_mode/fetch_concurrency/
    timed_out/retried — additive only, backward compatible."""

    def test_fetch_stats_default_to_sequential_when_omitted(self):
        from market.services.dse_fetcher import sync_dse_history

        with mock.patch("market.services.dse_fetcher.fetch_dse_history", side_effect=lambda c, s, e, **kw: (None, "none")):
            result = sync_dse_history(codes=["A"], use_synthetic_fallback=False)
        self.assertEqual(result["fetch_mode"], "sequential")
        self.assertEqual(result["fetch_concurrency"], 1)
        self.assertEqual(result["timed_out"], 0)
        self.assertEqual(result["retried"], 0)

    def test_fetch_stats_merged_when_provided(self):
        from market.services.dse_fetcher import sync_dse_history

        result = sync_dse_history(
            codes=["A"],
            use_synthetic_fallback=False,
            _prefetched=[("A", _df(), "archive")],
            _fetch_stats={"mode": "threadpool", "concurrency": 5, "timed_out": 2, "retried": 3},
        )
        self.assertEqual(result["fetch_mode"], "threadpool")
        self.assertEqual(result["fetch_concurrency"], 5)
        self.assertEqual(result["timed_out"], 2)
        self.assertEqual(result["retried"], 3)
