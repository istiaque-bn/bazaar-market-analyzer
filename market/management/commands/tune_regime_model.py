import json

from django.core.management.base import BaseCommand

from market.models import Exchange
from market.services.regime_tuning import tune_regime_challenger


class Command(BaseCommand):
    help = (
        "Run a bounded, coverage-constrained hyperparameter search for the "
        "regime-routed forward-return challenger. Tunes on validation, judges "
        "once on an untouched test window, and never activates any model."
    )

    def add_arguments(self, parser):
        parser.add_argument("--exchange", default=Exchange.DSE)
        parser.add_argument("--limit", type=int, default=120)
        parser.add_argument("--min-coverage", type=float, default=0.30)
        parser.add_argument(
            "--persist",
            action="store_true",
            help="Record the selected config as an experimental (inactive) model version.",
        )

    def handle(self, *args, **options):
        result = tune_regime_challenger(
            exchange=options["exchange"],
            limit_stocks=options["limit"],
            min_coverage=options["min_coverage"],
            persist=options["persist"],
        )
        self.stdout.write(json.dumps(result, indent=2, default=str))
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "regime tuning failed"))
