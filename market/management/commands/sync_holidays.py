import json

from django.core.management.base import BaseCommand

from market.services.holiday_sync import sync_holiday_calendar


class Command(BaseCommand):
    help = "Fetch DSE's published holiday notice and upsert market.models.MarketHoliday (see market/services/holiday_sync.py)."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Print raw JSON instead of a summary.")

    def handle(self, *args, **options):
        result = sync_holiday_calendar()
        if options["json"]:
            self.stdout.write(json.dumps(result, default=str, indent=2))
            return
        if not result["ok"]:
            self.stderr.write(self.style.ERROR(f"Holiday sync failed: {result['error']}"))
            return
        self.stdout.write(
            self.style.SUCCESS(
                f"DSE holiday calendar for {result['year']}: {result['created']} added, "
                f"{result['updated']} renamed, {result['unchanged']} unchanged "
                f"({result['parsed_entries']} rows parsed)."
            )
        )
