import json

from django.core.management.base import BaseCommand

from market.models import Exchange
from market.services.autosync import exclusive_db_write
from market.services.reliability_report import run_reliability_assessment


class Command(BaseCommand):
    help = (
        "Run the ML Reliability Monitor: capture today's predictions, settle due outcomes, and assess "
        "forward_return_rf / next_close_rf against rolling windows. Never activates/deactivates a model "
        "itself — see the printed recommendations for what a human should consider doing next."
    )

    def add_arguments(self, parser):
        parser.add_argument("--model", choices=["forward_return_rf", "next_close_rf"], help="Limit to one model family.")
        parser.add_argument("--exchange", choices=["DSE", "CSE"], help="Limit to one exchange.")
        parser.add_argument("--window", type=int, help="Limit to one rolling window size (e.g. 30, 90, 180, 365).")
        parser.add_argument(
            "--model-version",
            dest="model_version",
            help="Evaluate one specific MLModelVersion.version tag instead of whichever is currently active "
            "(named --model-version, not --version, since Django's BaseCommand already reserves --version "
            "for its own 'show Django version' flag).",
        )
        parser.add_argument("--json", action="store_true", help="Print the full structured assessment as JSON.")
        parser.add_argument("--settle-only", action="store_true", help="Only capture + settle predictions; skip metrics/assessment.")
        parser.add_argument("--dry-run", action="store_true", help="Read-only preview: no capture/settlement writes, no ReliabilityAssessment rows persisted.")

    def handle(self, *args, **options):
        families = [options["model"]] if options["model"] else None
        exchanges = [options["exchange"]] if options["exchange"] else [Exchange.DSE, Exchange.CSE]
        windows = [options["window"]] if options["window"] else None

        with exclusive_db_write(blocking=True, timeout=300):
            result = run_reliability_assessment(
                families=families,
                exchanges=exchanges,
                windows=windows,
                version_tag=options["model_version"],
                settle_only=options["settle_only"],
                dry_run=options["dry_run"],
            )

        if options["json"]:
            self.stdout.write(json.dumps(result, default=str, indent=2))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(f"-- ML Reliability Monitor · as_of={result['as_of']} --"))
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("DRY RUN — no capture/settlement/assessment rows were written."))
        cap = result.get("capture", {})
        settle = result.get("settlement", {})
        if not options["dry_run"] and not options["settle_only"]:
            self.stdout.write(
                f"Captured: forward_return_rf +{cap.get('forward_return_rf', {}).get('created', 0)}, "
                f"next_close_rf +{cap.get('next_close_rf', {}).get('created', 0)}"
            )
        if not options["dry_run"]:
            self.stdout.write(
                f"Settled: {settle.get('settled', 0)}  Excluded: {settle.get('excluded', 0)}  "
                f"Still pending: {settle.get('still_pending', 0)}"
            )

        if options["settle_only"]:
            return

        for a in result.get("assessments", []):
            status_style = self.style.SUCCESS if a["status"] == "healthy" else (
                self.style.ERROR if a["status"] in ("degraded", "critical") else self.style.WARNING
            )
            self.stdout.write(
                f"\n{a['model_family']:18s} [{a['exchange']}] h={a['horizon_trading_days']:>2} w={a['window_label']:>3}  "
                f"n={a['sample_count']:4d}  " + status_style(a["status"].upper())
            )
            for reason in a["reasons"]:
                self.stdout.write(f"    - {reason}")
            for rec in a["recommendations"]:
                self.stdout.write(f"    -> {rec['action']}: {rec['reason']}")

        for flag in result.get("cross_exchange_flags", []):
            self.stdout.write(self.style.WARNING(f"\n[{flag['model_family']} h={flag['horizon_trading_days']} w={flag['window_label']}] {flag['reason']}"))

        self.stdout.write(
            self.style.WARNING(
                "\nThese are statistical evaluations of past settled predictions, not guarantees of future "
                "performance. 'Healthy' means no demonstrated problem was found — not 'safe' or 'profitable'. "
                "See docs/RUNBOOKS.md for how to investigate and act on any status other than healthy."
            )
        )
