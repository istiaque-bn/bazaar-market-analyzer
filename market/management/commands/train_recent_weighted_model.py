import json

from django.core.management.base import BaseCommand

from market.services.recent_weighted_model import train_recent_weighted_challenger


class Command(BaseCommand):
    help = "Train and compare the research-only recent-weighted forward-return challenger."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=120)
        parser.add_argument("--no-persist", action="store_true")

    def handle(self, *args, **options):
        result = train_recent_weighted_challenger(limit_stocks=options["limit"], persist=not options["no_persist"])
        self.stdout.write(json.dumps(result, indent=2, default=str))
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "research model training failed"))
