import json

from django.core.management.base import BaseCommand

from market.services.dse_sector_sync import sync_dse_sector_classification


class Command(BaseCommand):
    help = "Fetch DSE's sector directory and upsert Stock.sector/company_name for matched trading codes (see market/services/dse_sector_sync.py)."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Print raw JSON instead of a summary.")

    def handle(self, *args, **options):
        result = sync_dse_sector_classification()
        if options["json"]:
            self.stdout.write(json.dumps(result, default=str, indent=2))
            return
        if not result["ok"]:
            self.stderr.write(self.style.ERROR(f"DSE sector sync failed: {result['error']}"))
            return
        self.stdout.write(
            self.style.SUCCESS(
                f"DSE sector sync: {result['sectors_synced']}/{result['sectors_discovered']} sector pages synced "
                f"({result['sectors_failed']} failed), {result['updated']} stocks updated, "
                f"{result['unchanged']} unchanged, {result['unmatched']} DSE codes not found locally."
            )
        )
        if result["unmatched_codes_sample"]:
            self.stdout.write(f"  Sample unmatched codes: {', '.join(result['unmatched_codes_sample'])}")
