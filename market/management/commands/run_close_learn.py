from django.core.management.base import BaseCommand

from market.services.autosync import exclusive_db_write
from market.services.close_learn import (
    backfill_learn_from_history,
    generate_forecasts_for_as_of,
    learn_status,
    run_close_learn_cycle,
    settle_due_forecasts,
    train_next_close_model,
)


class Command(BaseCommand):
    help = "Next-day close learn loop: forecast after close, settle vs actual close, retrain."

    def add_arguments(self, parser):
        parser.add_argument(
            "--cycle",
            action="store_true",
            help="Settle due forecasts, retrain ML, generate next-day forecasts (as_of=today).",
        )
        parser.add_argument("--forecast-only", action="store_true", help="Only write next-day forecasts.")
        parser.add_argument("--settle-only", action="store_true", help="Only settle vs actual closes.")
        parser.add_argument("--train-only", action="store_true", help="Only train next-close RF model.")
        parser.add_argument(
            "--backfill",
            action="store_true",
            help="Simulate learn loop on recent history to seed bias/ML.",
        )
        parser.add_argument("--lookback", type=int, default=60, help="Days for --backfill (default 60).")
        parser.add_argument("--limit", type=int, default=40, help="Stock limit for backfill/train.")
        parser.add_argument("--status", action="store_true", help="Print learn status JSON.")

    def handle(self, *args, **options):
        if options["status"] or not any(
            [
                options["cycle"],
                options["forecast_only"],
                options["settle_only"],
                options["train_only"],
                options["backfill"],
            ]
        ):
            self.stdout.write(str(learn_status()))
            if not any(
                [
                    options["cycle"],
                    options["forecast_only"],
                    options["settle_only"],
                    options["train_only"],
                    options["backfill"],
                ]
            ):
                self.stdout.write(self.style.WARNING("Pass --cycle, --backfill, etc. (see --help)."))
                return

        with exclusive_db_write(blocking=True, timeout=600):
            if options["backfill"]:
                result = backfill_learn_from_history(
                    lookback_days=options["lookback"], limit_stocks=options["limit"]
                )
                self.stdout.write(self.style.SUCCESS(f"Backfill: {result}"))
            if options["settle_only"]:
                self.stdout.write(self.style.SUCCESS(f"Settle: {settle_due_forecasts()}"))
            if options["train_only"]:
                self.stdout.write(self.style.SUCCESS(f"Train: {train_next_close_model(options['limit'])}"))
            if options["forecast_only"]:
                self.stdout.write(self.style.SUCCESS(f"Forecast: {generate_forecasts_for_as_of()}"))
            if options["cycle"]:
                self.stdout.write(self.style.SUCCESS(f"Cycle: {run_close_learn_cycle(train=True)}"))
        self.stdout.write(str(learn_status()))
