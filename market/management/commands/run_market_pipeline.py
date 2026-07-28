from django.core.management.base import BaseCommand

from market.services.analyzer import fetch_all, run_full_analysis
from market.services.dse_fetcher import seed_demo_universe


class Command(BaseCommand):
    help = "Seed demo data and/or fetch + analyze DSE/CSE markets (phases 1-5)."

    def add_arguments(self, parser):
        parser.add_argument("--demo", action="store_true", help="Seed synthetic 1y history for demo stocks")
        parser.add_argument("--fetch", action="store_true", help="Fetch live/history from DSE/CSE")
        parser.add_argument("--analyze", action="store_true", help="Run full analysis + ML + backtests")
        parser.add_argument("--all", action="store_true", help="Demo + fetch + analyze")

    def handle(self, *args, **options):
        run_all = options["all"]
        if options["demo"] or run_all:
            result = seed_demo_universe()
            self.stdout.write(self.style.SUCCESS(f"Demo seeded: {result}"))
        if options["fetch"] or run_all:
            result = fetch_all(use_demo_if_empty=True, include_history=True)
            self.stdout.write(self.style.SUCCESS(f"Fetch complete: {result}"))
        if options["analyze"] or run_all:
            result = run_full_analysis(train_ml=True)
            self.stdout.write(self.style.SUCCESS(f"Analysis complete: {result}"))
        if not any([options["demo"], options["fetch"], options["analyze"], run_all]):
            self.stdout.write("Use --demo, --fetch, --analyze, or --all")
