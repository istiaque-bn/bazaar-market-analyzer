import json

from django.core.management.base import BaseCommand

from market.services.ops_alerts import evaluate_alerts


class Command(BaseCommand):
    help = "Phase 9: evaluate operational alert thresholds (stale data, repeated fetch failure, job overlap/stuck jobs, database problems, model degradation)."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Print raw JSON instead of a summary.")
        parser.add_argument("--fail-on-critical", action="store_true", help="Exit 1 if any critical alert is firing (for CI/cron use).")

    def handle(self, *args, **options):
        alerts = evaluate_alerts()
        if options["json"]:
            self.stdout.write(json.dumps(alerts, default=str, indent=2))
        elif not alerts:
            self.stdout.write(self.style.SUCCESS("No alerts firing."))
        else:
            for a in alerts:
                style = self.style.ERROR if a["severity"] == "critical" else self.style.WARNING
                self.stdout.write(style(f"[{a['severity'].upper()}] {a['key']}: {a['message']}"))

        if options["fail_on_critical"] and any(a["severity"] == "critical" for a in alerts):
            raise SystemExit(1)
