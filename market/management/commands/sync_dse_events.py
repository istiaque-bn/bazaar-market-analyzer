import json

from django.core.management.base import BaseCommand

from market.services.dse_events import fetch_dse_agm_egm_pdf, parse_agm_egm_pdf, sync_dse_agm_egm_events


class Command(BaseCommand):
    help = "Fetch DSE's published AGM/EGM/Record Date PDF and upsert market.models.MarketEvent (see market/services/dse_events.py)."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Print raw JSON instead of a summary.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Fetch and parse only — print what would happen, write nothing to the database.",
        )

    def handle(self, *args, **options):
        if options["dry_run"]:
            pdf_bytes = fetch_dse_agm_egm_pdf()
            rows = parse_agm_egm_pdf(pdf_bytes)
            with_agm = sum(1 for r in rows if r["agm_egm_date"] is not None)
            with_record = sum(1 for r in rows if r["record_date"] is not None)
            self.stdout.write(f"Parsed {len(rows)} company rows from the PDF ({len(pdf_bytes):,} bytes, not saved).")
            self.stdout.write(f"  {with_agm} would produce an AGM/EGM event; {with_record} would produce a Record Date event.")
            self.stdout.write("\nSample rows:")
            for r in rows[:5]:
                self.stdout.write(
                    f"  {r['company']} | AGM/EGM: {r['agm_egm_date'] or '(none yet)'} | "
                    f"Record: {r['record_date'] or '(none yet)'} | {r['purpose'][:60]}"
                )
            return

        result = sync_dse_agm_egm_events()
        if options["json"]:
            self.stdout.write(json.dumps(result, default=str, indent=2))
            return
        if not result["ok"]:
            self.stderr.write(self.style.ERROR(f"DSE event sync failed: {result['error']}"))
            return
        self.stdout.write(
            self.style.SUCCESS(
                f"DSE AGM/EGM/Record Date sync: {result['created']} created, {result['updated']} updated, "
                f"{result['unchanged']} unchanged ({result['parsed_rows']} company rows parsed, "
                f"{result['skipped_no_date']} date fields not yet announced)."
            )
        )
