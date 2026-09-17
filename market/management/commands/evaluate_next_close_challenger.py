import json

from django.core.management.base import BaseCommand

from market.models import Exchange
from market.services.close_learn import _build_next_close_panel
from market.services.next_close_challenger import evaluate_locked_holdout


class Command(BaseCommand):
    help = "Read-only chronological and locked-holdout evaluation of the selective next-close challenger."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=80, help="DSE stock limit (default 80).")

    def handle(self, *args, **options):
        panel = _build_next_close_panel(Exchange.DSE, limit_stocks=options["limit"])
        self.stdout.write(json.dumps(evaluate_locked_holdout(panel), default=str, indent=2))
