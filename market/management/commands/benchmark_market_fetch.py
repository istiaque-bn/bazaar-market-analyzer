"""Benchmark sequential vs. threadpool vs. asyncio DSE history fetching
against real dsebd.org — no DB writes, network phase only (see
market.services.concurrent_fetch.prefetch_dse_history). Throwaway tooling:
not wired into any Celery task or scheduled job.

Usage:
  python manage.py benchmark_market_fetch --codes 20 \\
      --modes sequential,threadpool,asyncio --concurrency 3,5,8,10 --runs 3
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from market.models import Exchange, Stock
from market.services.concurrent_fetch import prefetch_dse_history


class Command(BaseCommand):
    help = "Benchmark DSE history fetch modes (sequential/threadpool/asyncio) against real dsebd.org."

    def add_arguments(self, parser):
        parser.add_argument("--codes", type=int, default=20, help="Number of real active DSE symbols to sample (default 20)")
        parser.add_argument("--modes", type=str, default="sequential,threadpool,asyncio", help="Comma-separated modes to test")
        parser.add_argument("--concurrency", type=str, default="5", help="Comma-separated concurrency levels (ignored for sequential)")
        parser.add_argument("--runs", type=int, default=1, help="Repeat each (mode, concurrency) this many times")
        parser.add_argument("--days", type=int, default=730, help="Lookback window in days (default ~2y, matching typical real coverage)")

    def handle(self, *args, **options):
        n_codes = options["codes"]
        modes = [m.strip() for m in options["modes"].split(",") if m.strip()]
        concurrency_levels = [int(c.strip()) for c in options["concurrency"].split(",") if c.strip()]
        runs = options["runs"]
        end = timezone.localdate()
        start = end - timedelta(days=options["days"])

        codes = list(
            Stock.objects.filter(exchange=Exchange.DSE, is_active=True)
            .order_by("trading_code")
            .values_list("trading_code", flat=True)[:n_codes]
        )
        if not codes:
            self.stdout.write(self.style.ERROR("No active DSE stocks in the DB — seed the universe first (e.g. `manage.py fetch_history`)."))
            return
        self.stdout.write(f"Benchmarking {len(codes)} real DSE symbols: {', '.join(codes[:5])}{'…' if len(codes) > 5 else ''}\n")

        rows = []
        for mode in modes:
            levels = concurrency_levels if mode != "sequential" else [1]
            for concurrency in levels:
                for run_i in range(1, runs + 1):
                    _, stats = prefetch_dse_history(codes, start, end, mode=mode, concurrency=concurrency)
                    rows.append({"mode": mode, "concurrency": concurrency, "run": run_i, **stats})
                    self.stdout.write(
                        f"  [{mode:10s} c={concurrency:2d} run={run_i}] "
                        f"{stats['successful']}/{stats['attempted']} ok in {stats['duration_seconds']:7.2f}s "
                        f"(failed={stats['failed']} timed_out={stats['timed_out']} retried={stats['retried']} "
                        f"rate_limited={stats['rate_limited_symbols']})"
                    )

        self.stdout.write("\n" + self.style.SUCCESS("--- Summary (fastest first) ---"))
        for row in sorted(rows, key=lambda r: r["duration_seconds"]):
            self.stdout.write(
                f"  {row['duration_seconds']:7.2f}s  mode={row['mode']:10s} concurrency={row['concurrency']:2d} "
                f"run={row['run']}  ok={row['successful']}/{row['attempted']}  "
                f"failed={row['failed']} timed_out={row['timed_out']}"
            )

        baseline = next((r for r in rows if r["mode"] == "sequential"), None)
        if baseline:
            self.stdout.write(f"\nSequential baseline: {baseline['duration_seconds']:.2f}s for {baseline['attempted']} symbols.")
            for row in rows:
                if row["mode"] == "sequential":
                    continue
                speedup = baseline["duration_seconds"] / row["duration_seconds"] if row["duration_seconds"] else float("inf")
                completeness_ok = row["successful"] >= baseline["successful"]
                verdict = "OK" if completeness_ok and row["failed"] <= baseline["failed"] and row["rate_limited_symbols"] == 0 else "REVIEW"
                self.stdout.write(
                    f"  {row['mode']:10s} c={row['concurrency']:2d}: {speedup:.2f}x speedup, "
                    f"completeness {'preserved' if completeness_ok else 'REDUCED'}  [{verdict}]"
                )
