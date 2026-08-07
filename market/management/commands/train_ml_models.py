import json

from django.core.management.base import BaseCommand

from market.services.autosync import exclusive_db_write
from market.services.close_learn import train_next_close_model
from market.services.ml_model import train_model


def _print_walk_forward(stdout, style, label: str, evaluation: dict):
    stdout.write(style.MIGRATE_HEADING(f"-- {label} --"))
    for scope in ("dse", "cse", "combined"):
        ev = evaluation.get(scope, {})
        if not ev.get("ok"):
            stdout.write(f"  {scope.upper():8s} not evaluated: {ev.get('error')}")
            continue
        m = ev["model_metrics"]
        skill = ev.get("skill_vs_naive")
        stdout.write(
            f"  {scope.upper():8s} n={m['n']:5d}  acc={m.get('accuracy', m.get('mae'))}  "
            f"dir_hit={m.get('direction_hit_rate')}  skill_vs_naive={skill}"
        )
    stdout.write(f"  combine_justified={evaluation.get('combine_justified')} — {evaluation.get('combine_reason')}")


class Command(BaseCommand):
    help = (
        "Retrain both ML models (forward-return classifier, next-day direction classifier) using "
        "chronological walk-forward validation and print a walk-forward evaluation report. "
        "Models are only deployed active if out-of-sample skill vs. their naive baseline is positive."
    )

    def add_arguments(self, parser):
        parser.add_argument("--forward-return-only", action="store_true", help="Only retrain the 10d forward-return classifier.")
        parser.add_argument("--next-close-only", action="store_true", help="Only retrain the next-day direction model.")
        parser.add_argument("--limit", type=int, default=120, help="Stock limit per exchange for training.")
        parser.add_argument("--json", action="store_true", help="Print the full raw result as JSON instead of a summary.")

    def handle(self, *args, **options):
        do_forward = not options["next_close_only"]
        do_next_close = not options["forward_return_only"]
        results = {}

        with exclusive_db_write(blocking=True, timeout=600):
            if do_forward:
                # force=True: running this command is an explicit, deliberate
                # request to retrain right now, unlike the automatic paths
                # (dashboard "Analyze" button, run_full_analysis) that should
                # skip a redundant retrain when nothing new has arrived.
                fr = train_model(limit_stocks=options["limit"], force=True)
                results["forward_return_rf"] = fr
                if options["json"]:
                    self.stdout.write(json.dumps(fr, default=str, indent=2))
                elif fr.get("ok"):
                    _print_walk_forward(self.stdout, self.style, "forward_return_rf (10-day forward return)", fr["evaluation"])
                    self.stdout.write(self.style.SUCCESS(f"  deployed: {fr['deployed']}"))
                else:
                    self.stdout.write(self.style.ERROR(f"forward_return_rf: {fr.get('error')}"))

            if do_next_close:
                nc = train_next_close_model(limit_stocks=options["limit"])
                results["next_close_rf"] = nc
                if options["json"]:
                    self.stdout.write(json.dumps(nc, default=str, indent=2))
                elif nc.get("ok"):
                    _print_walk_forward(self.stdout, self.style, "next_close_rf (1-day close return)", nc["evaluation"])
                    self.stdout.write(self.style.SUCCESS(f"  deployed: {nc['deployed']}"))
                else:
                    self.stdout.write(self.style.ERROR(f"next_close_rf: {nc.get('error')}"))

        self.stdout.write(
            self.style.WARNING(
                "\nWalk-forward metrics are estimates from a limited BD-market lookback window, not "
                "guarantees of future performance. A model saved as 'experimental' is not used for "
                "scoring — predictions fall back to the pure rule-based estimate."
            )
        )
