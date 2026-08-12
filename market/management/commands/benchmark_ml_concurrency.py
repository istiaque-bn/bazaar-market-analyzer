"""Benchmark ML training wall-clock time vs. its impact on concurrent
"web-like" DB query latency, across candidate settings.ML_MAX_WORKERS
values — measures whether capping XGBoost/RandomForest's n_jobs (previously
hard-coded -1, every core) trades training speed for web responsiveness,
and by how much. Throwaway tooling: not wired into any Celery task.

Usage:
  python manage.py benchmark_ml_concurrency --n-jobs -1,4,2,1 \\
      --target train_model --limit-stocks 40
"""
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from market.models import Stock


class _LatencyProbe:
    """Repeatedly runs a cheap, realistic DB read (roughly what a Gunicorn
    request handler does) in a background thread, recording each call's
    latency, so we can compare "web app" responsiveness with vs. without
    a CPU-heavy training job running concurrently."""

    def __init__(self, interval: float = 0.05):
        self.interval = interval
        self.latencies: list[float] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self):
        while not self._stop.is_set():
            started = time.monotonic()
            list(Stock.objects.filter(is_active=True).order_by("-last_change_pct")[:20].values("trading_code", "last_price"))
            self.latencies.append(time.monotonic() - started)
            time.sleep(self.interval)

    def start(self):
        self.latencies = []
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> list[float]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        return self.latencies


class _BenchmarkRollback(Exception):
    """Sentinel used to force-rollback every DB write a benchmarked
    training run makes — this command must never deploy a model version
    or flip which model is 'active' in a real database, only measure
    wall-clock cost."""


def _run_isolated(fn, *args, **kwargs):
    """Runs fn(*args, **kwargs) (train_model / train_next_close_model)
    with its MLModelVersion / CloseLearnState writes rolled back and its
    .pkl artifact writes redirected to a temp directory. Safe to point at
    a real production database — nothing it does outlives this call."""
    from market.models import Exchange
    from market.services import close_learn, ml_model

    with tempfile.TemporaryDirectory() as td:
        tmp_by_exchange = {Exchange.DSE: Path(td) / "dse.pkl", Exchange.CSE: Path(td) / "cse.pkl"}
        tmp_nc_by_exchange = {Exchange.DSE: Path(td) / "nc_dse.pkl", Exchange.CSE: Path(td) / "nc_cse.pkl"}
        with mock.patch.object(ml_model, "MODEL_PATH", Path(td) / "combined.pkl"), \
                mock.patch.object(ml_model, "MODEL_PATH_BY_EXCHANGE", tmp_by_exchange), \
                mock.patch.object(close_learn, "MODEL_PATH", Path(td) / "next_close.pkl"), \
                mock.patch.object(close_learn, "MODEL_PATH_BY_EXCHANGE", tmp_nc_by_exchange):
            result = None
            try:
                with transaction.atomic():
                    result = fn(*args, **kwargs)
                    raise _BenchmarkRollback()
            except _BenchmarkRollback:
                pass
    return result


def _pctile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(len(values) - 1, int(len(values) * p))
    return values[idx]


class Command(BaseCommand):
    help = "Benchmark ML training time vs. concurrent web-query latency impact across ML_MAX_WORKERS candidates."

    def add_arguments(self, parser):
        parser.add_argument("--n-jobs", type=str, default="-1,4,2,1", help="Comma-separated n_jobs candidates to test")
        parser.add_argument("--target", choices=("train_model", "train_next_close_model", "both"), default="train_model")
        parser.add_argument("--limit-stocks", type=int, default=40, help="Stocks to include (smaller = faster benchmark)")

    def handle(self, *args, **options):
        from market.services import close_learn, ml_model

        candidates = [int(x.strip()) for x in options["n_jobs"].split(",") if x.strip()]
        targets = ["train_model", "train_next_close_model"] if options["target"] == "both" else [options["target"]]
        limit_stocks = options["limit_stocks"]

        self.stdout.write("--- Quiet baseline (no training running) ---")
        probe = _LatencyProbe()
        probe.start()
        time.sleep(3)
        baseline = probe.stop()
        base_p50, base_p95 = _pctile(baseline, 0.5), _pctile(baseline, 0.95)
        self.stdout.write(f"  baseline DB-query latency: p50={base_p50*1000:.1f}ms p95={base_p95*1000:.1f}ms (n={len(baseline)})\n")

        rows = []
        for target in targets:
            fn = ml_model.train_model if target == "train_model" else close_learn.train_next_close_model
            kwargs = {"limit_stocks": limit_stocks, "force": True} if target == "train_model" else {"limit_stocks": limit_stocks}
            for n_jobs in candidates:
                # n_jobs=-1 (the OLD, uncapped behavior) is reproduced via a
                # direct settings override — ML_MAX_WORKERS itself must stay
                # positive for _positive_int's own validation, but scikit-
                # learn/xgboost accept -1 as "every core" regardless of
                # source, so patching the attribute value works either way.
                with mock.patch.object(settings, "ML_MAX_WORKERS", n_jobs):
                    probe = _LatencyProbe()
                    probe.start()
                    started = time.monotonic()
                    _run_isolated(fn, **kwargs)
                duration = time.monotonic() - started
                during = probe.stop()
                p50, p95, p_max = _pctile(during, 0.5), _pctile(during, 0.95), max(during) if during else 0.0
                rows.append({"target": target, "n_jobs": n_jobs, "duration": duration, "p50": p50, "p95": p95, "max": p_max, "n": len(during)})
                self.stdout.write(
                    f"  [{target} n_jobs={n_jobs:3d}] train={duration:6.1f}s  "
                    f"during-train DB latency: p50={p50*1000:6.1f}ms p95={p95*1000:6.1f}ms max={p_max*1000:6.1f}ms "
                    f"(baseline p50={base_p50*1000:.1f}ms, {p50/base_p50 if base_p50 else 0:.1f}x)"
                )

        self.stdout.write("\n" + self.style.SUCCESS("--- Summary ---"))
        for row in sorted(rows, key=lambda r: (r["target"], r["duration"])):
            degradation = row["p95"] / base_p95 if base_p95 else float("inf")
            self.stdout.write(
                f"  {row['target']:22s} n_jobs={row['n_jobs']:3d}  train={row['duration']:6.1f}s  "
                f"p95_latency={row['p95']*1000:6.1f}ms ({degradation:.1f}x baseline)"
            )
