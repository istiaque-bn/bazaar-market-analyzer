from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from market.models import Exchange, Stock
from market.services.analyzer import run_full_analysis
from market.services.autosync import configure_sqlite, exclusive_db_write
from market.services.cse_fetcher import fetch_cse_history_bulk, sync_cse_history
from market.services.dse_fetcher import fetch_dse_history, sync_dse_history


class Command(BaseCommand):
    help = "Fetch historical OHLC for DSE and CSE stocks (real data). Use --years 5 for a 5y request."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=1825,
            help="Lookback calendar days (default 1825 ≈ 5y; public sources often only serve ~2y)",
        )
        parser.add_argument("--limit", type=int, default=0, help="Optional max symbols per exchange (0 = all)")
        parser.add_argument("--codes", type=str, default="", help="Comma-separated trading codes (both exchanges)")
        parser.add_argument(
            "--exchange",
            choices=("both", "dse", "cse"),
            default="both",
            help="Which exchange(s) to fetch (default: both)",
        )
        parser.add_argument("--analyze", action="store_true", help="Run analysis after fetch")
        parser.add_argument(
            "--years",
            type=float,
            default=0,
            help="Convenience: years lookback (overrides --days when set)",
        )

    def handle(self, *args, **options):
        configure_sqlite()
        codes = [c.strip().upper() for c in options["codes"].split(",") if c.strip()] or None
        limit = options["limit"] or None
        days = options["days"]
        if options["years"]:
            days = int(round(float(options["years"]) * 365.25))
        exchange = options["exchange"]
        end = timezone.localdate()
        start = end - timedelta(days=days)

        targets = []
        if exchange in ("both", "dse"):
            targets.append(("DSE", Exchange.DSE))
        if exchange in ("both", "cse"):
            targets.append(("CSE", Exchange.CSE))

        for label, ex in targets:
            if codes is None and limit is None:
                n = Stock.objects.filter(exchange=ex, is_active=True).count()
                self.stdout.write(
                    f"Fetching up to ~{days}d (~{days / 365:.1f}y) history for {n} {label} stocks…"
                )
            else:
                self.stdout.write(f"Fetching up to ~{days}d {label} history…")
            self.stdout.write(
                self.style.WARNING(
                    f"Note: {label} public feeds typically only expose ~last 2 years."
                )
            )

        results = {}

        # --- Network phase (no SQLite write lock) ---
        dse_payloads = None
        cse_bulk = None
        if exchange in ("both", "dse"):
            dse_codes = codes
            if dse_codes is None:
                qs = Stock.objects.filter(exchange=Exchange.DSE, is_active=True).values_list("trading_code", flat=True)
                if limit:
                    qs = qs[:limit]
                dse_codes = list(qs)
            self.stdout.write(f"Downloading DSE history for {len(dse_codes)} symbols…")
            dse_payloads = []
            for i, code in enumerate(dse_codes, start=1):
                df, source = fetch_dse_history(code, start, end)
                dse_payloads.append((code, df, source))
                if i % 25 == 0 or i == len(dse_codes):
                    self.stdout.write(f"  DSE download {i}/{len(dse_codes)}")

        if exchange in ("both", "cse"):
            self.stdout.write("Downloading CSE bulk history export…")
            cse_bulk = fetch_cse_history_bulk(start, end)
            if cse_bulk is not None and not cse_bulk.empty:
                self.stdout.write(
                    f"  CSE bulk: {len(cse_bulk)} rows, {cse_bulk['symbol'].nunique()} symbols, "
                    f"{cse_bulk['date'].min()} → {cse_bulk['date'].max()}"
                )
            else:
                self.stdout.write(self.style.WARNING("  CSE bulk download failed or empty"))

        # --- Persist phase (exclusive SQLite write) ---
        with exclusive_db_write(blocking=True, timeout=3600):
            if exchange in ("both", "dse"):
                results["dse"] = sync_dse_history(
                    codes=codes,
                    lookback_days=days,
                    use_synthetic_fallback=False,
                    limit=limit,
                    _prefetched=dse_payloads,
                )
            if exchange in ("both", "cse"):
                results["cse"] = sync_cse_history(
                    codes=codes,
                    lookback_days=days,
                    use_synthetic_fallback=False,
                    limit=limit,
                    _prefetched_bulk=cse_bulk,
                )

        self.stdout.write(self.style.SUCCESS(f"History fetch done: {results}"))

        if options["analyze"]:
            with exclusive_db_write(blocking=True, timeout=600):
                analysis = run_full_analysis(train_ml=True)
            self.stdout.write(self.style.SUCCESS(f"Analysis done: {analysis}"))
