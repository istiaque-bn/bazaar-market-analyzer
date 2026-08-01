import json

from django.core.management.base import BaseCommand

from market.services.data_quality import provenance_report, run_quality_scan


class Command(BaseCommand):
    help = "Run the Phase 6 data-quality scan (flags impossible OHLC, abnormal jumps, stale runs, gaps) and/or print the provenance report."

    def add_arguments(self, parser):
        parser.add_argument("--exchange", choices=["DSE", "CSE"], help="Limit the scan to one exchange.")
        parser.add_argument("--limit", type=int, help="Limit number of stocks scanned (debugging).")
        parser.add_argument("--report-only", action="store_true", help="Skip the scan, just print the provenance report.")
        parser.add_argument("--json", action="store_true", help="Print raw JSON instead of a summary.")

    def handle(self, *args, **options):
        if not options["report_only"]:
            result = run_quality_scan(exchange=options["exchange"], limit_stocks=options["limit"])
            if options["json"]:
                self.stdout.write(json.dumps(result, default=str, indent=2))
            else:
                self.stdout.write(self.style.SUCCESS(f"Scanned {result['stocks_scanned']} stocks / {result['rows_scanned']} rows"))
                self.stdout.write(f"Rows flagged: {result['rows_flagged']}  Gaps found: {result['gaps_found']}")
                for flag, n in sorted(result["flag_counts"].items(), key=lambda kv: -kv[1]):
                    self.stdout.write(f"  {flag}: {n}")

        report = provenance_report()
        if options["json"]:
            self.stdout.write(json.dumps(report, default=str, indent=2))
        else:
            self.stdout.write(self.style.MIGRATE_HEADING("\n-- Provenance report --"))
            self.stdout.write(f"Total rows: {report['total_rows']}")
            self.stdout.write(f"Synthetic/demo rows: {report['synthetic_rows']}")
            self.stdout.write(f"Unknown-provenance rows: {report['unknown_provenance_rows']}")
            self.stdout.write(f"Flagged rows: {report['flagged_rows']}")
            self.stdout.write("By source:")
            for source, n in sorted(report["by_source"].items(), key=lambda kv: -kv[1]):
                self.stdout.write(f"  {source}: {n}")
            self.stdout.write("Freshness:")
            for ex, f in report["freshness"].items():
                self.stdout.write(f"  {ex}: latest_price_date={f['latest_price_date']} last_batch={f['last_successful_batch_at']} ({f['last_successful_batch_source']})")
