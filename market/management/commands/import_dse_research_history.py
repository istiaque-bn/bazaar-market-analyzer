from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from market.models import AdjustmentStatus, DataSource, Exchange, ImportBatch, PriceHistory, Stock


class Command(BaseCommand):
    help = "Import validated, unadjusted DSE research history without overwriting existing rows."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--start", default="2000-01-01")
        parser.add_argument("--end", default="2024-07-27")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        path = Path(options["csv_path"])
        if not path.is_file():
            raise CommandError(f"CSV not found: {path}")
        start = date.fromisoformat(options["start"])
        end = date.fromisoformat(options["end"])
        if start > end:
            raise CommandError("--start must be on or before --end")

        stocks = {s.trading_code.upper(): s for s in Stock.objects.filter(exchange=Exchange.DSE)}
        if not stocks:
            raise CommandError("No DSE stocks exist in the database")

        accepted = invalid = unknown_ticker = out_of_range = 0
        rows: list[PriceHistory] = []
        batch_size = 5000
        import_batch = None
        if not options["dry_run"]:
            import_batch = ImportBatch.objects.create(
                source=DataSource.DSE_RESEARCH_BACKFILL,
                exchange=Exchange.DSE,
                request_meta={
                    "dataset": "Mendeley Data DOI 10.17632/5mww8rb9td.1",
                    "license": "CC BY 4.0",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "filename": path.name,
                },
                notes="Unadjusted research backfill; existing (stock,date) rows preserved.",
            )

        def flush():
            nonlocal rows
            if rows and not options["dry_run"]:
                PriceHistory.objects.bulk_create(rows, batch_size=batch_size, ignore_conflicts=True)
            rows = []

        try:
            for chunk in pd.read_csv(path, chunksize=100_000):
                required = {"Trading_Code", "Date", "Open", "High", "Low", "Close", "Volume"}
                if not required.issubset(chunk.columns):
                    raise CommandError(f"Missing columns: {sorted(required - set(chunk.columns))}")
                chunk["Date"] = pd.to_datetime(chunk["Date"], errors="coerce").dt.date
                for record in chunk.itertuples(index=False):
                    d = record.Date
                    if not isinstance(d, date) or d < start or d > end:
                        out_of_range += 1
                        continue
                    stock = stocks.get(str(record.Trading_Code).strip().upper())
                    if stock is None:
                        unknown_ticker += 1
                        continue
                    try:
                        open_v, high_v, low_v, close_v = map(float, (record.Open, record.High, record.Low, record.Close))
                        volume = int(float(record.Volume))
                    except (TypeError, ValueError, OverflowError):
                        invalid += 1
                        continue
                    if min(open_v, high_v, low_v, close_v) <= 0 or volume < 0 or high_v < max(open_v, close_v, low_v) or low_v > min(open_v, close_v, high_v):
                        invalid += 1
                        continue
                    accepted += 1
                    if not options["dry_run"]:
                        rows.append(
                            PriceHistory(
                                stock=stock, date=d, open=open_v, high=high_v, low=low_v, close=close_v,
                                volume=volume, value=0, source=DataSource.DSE_RESEARCH_BACKFILL,
                                is_synthetic=False, fetched_at=timezone.now(), adjustment_status=AdjustmentStatus.RAW,
                                import_batch=import_batch,
                                quality_flags=["external_research_backfill", "unadjusted_prices", "traded_value_unavailable"],
                            )
                        )
                        if len(rows) >= batch_size:
                            flush()
            flush()
        except Exception as exc:
            if import_batch:
                import_batch.error = str(exc)[:2000]
                import_batch.finished_at = timezone.now()
                import_batch.save(update_fields=["error", "finished_at"])
            raise

        actual_rows = 0
        if import_batch:
            actual_rows = PriceHistory.objects.filter(import_batch=import_batch).count()
            import_batch.row_count = actual_rows
            import_batch.stock_count = PriceHistory.objects.filter(import_batch=import_batch).values("stock_id").distinct().count()
            import_batch.finished_at = timezone.now()
            import_batch.save(update_fields=["row_count", "stock_count", "finished_at"])

        result = {
            "dry_run": options["dry_run"], "eligible_rows": accepted, "inserted_rows": actual_rows,
            "invalid_rows": invalid, "unknown_ticker_rows": unknown_ticker, "out_of_range_rows": out_of_range,
        }
        self.stdout.write(str(result))
