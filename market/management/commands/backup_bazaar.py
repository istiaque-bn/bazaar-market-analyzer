"""Phase 9: scripted, recoverable backup of the database + trained ML
model artifacts. Writes a timestamped directory under backups/ (matching
the existing manual-backup convention already in this repo) containing
the data, a SHA256SUMS.txt for integrity checking, and a manifest.json
recording row counts at backup time — the reference verify_backup later
compares a restored copy against.

This command only ever *writes new files*. It never touches the live
db.sqlite3/model files beyond reading them, and never deletes anything
unless --prune-keep is passed explicitly.

Writing this backup is NOT the same as knowing it works — see
verify_backup.py and docs/RUNBOOKS.md's "Never claim a backup works
until a test restore is performed in isolation" rule.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prune_old_backups(root: Path, keep: int, stdout) -> None:
    """Delete the oldest timestamped backup dirs beyond `keep`. Only
    removes directories that already passed the SHA256SUMS.txt +
    manifest.json shape check (i.e. look like a real backup this command
    made) — never touches an unrelated directory a human left in
    backups/."""
    candidates = sorted(
        (d for d in root.iterdir() if d.is_dir() and (d / "manifest.json").exists() and (d / "SHA256SUMS.txt").exists()),
        key=lambda d: d.name,
    )
    to_delete = candidates[:-keep] if keep > 0 else candidates
    for d in to_delete:
        shutil.rmtree(d)
        stdout.write(f"Pruned old backup: {d}")


class Command(BaseCommand):
    help = "Back up the database and ML model artifacts to a timestamped directory. Run verify_backup on the result before trusting it."

    def add_arguments(self, parser):
        parser.add_argument("--dest", default=None, help="Backup root directory (default: BASE_DIR/backups)")
        parser.add_argument("--sqlite-path", default=None, help="Override which sqlite file to copy (mainly for tests)")
        parser.add_argument(
            "--prune-keep",
            type=int,
            default=None,
            help="After a successful backup, delete older backup dirs beyond this count (off by default — pruning is opt-in)",
        )

    def handle(self, *args, **options):
        engine = settings.DATABASES["default"]["ENGINE"]
        timestamp = timezone.now().strftime("%Y%m%d_%H%M%S")
        dest_root = Path(options["dest"] or (settings.BASE_DIR / "backups"))
        dest_root.mkdir(parents=True, exist_ok=True)
        backup_dir = dest_root / timestamp
        backup_dir.mkdir(parents=True, exist_ok=False)  # never silently merge into an existing timestamp

        manifest = {
            "created_at": timezone.now().isoformat(),
            "db_engine": engine,
            "files": [],
            "row_counts": {},
        }

        if "sqlite3" in engine:
            self._backup_sqlite(options, backup_dir, manifest)
        elif "postgresql" in engine:
            self._backup_postgres(backup_dir, manifest)
        else:
            self.stderr.write(self.style.ERROR(f"Unsupported DB engine for scripted backup: {engine}"))
            raise SystemExit(1)

        self._backup_model_artifacts(backup_dir, manifest)
        self._snapshot_row_counts(manifest)

        sums_path = backup_dir / "SHA256SUMS.txt"
        with open(sums_path, "w") as f:
            for name in manifest["files"]:
                f.write(f"{_sha256_file(backup_dir / name)}  {name}\n")

        (backup_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))

        self.stdout.write(self.style.SUCCESS(f"Backup written to {backup_dir}"))
        self.stdout.write(
            self.style.WARNING(
                f"NOT yet verified — run `python manage.py verify_backup {backup_dir}` before trusting this backup."
            )
        )

        if options["prune_keep"] is not None:
            _prune_old_backups(dest_root, options["prune_keep"], self.stdout)

    def _backup_sqlite(self, options, backup_dir: Path, manifest: dict) -> None:
        src = Path(options["sqlite_path"] or settings.DATABASES["default"]["NAME"])
        if not src.exists():
            self.stderr.write(self.style.ERROR(f"sqlite file not found: {src}"))
            raise SystemExit(1)
        dest = backup_dir / src.name
        shutil.copy2(src, dest)
        manifest["files"].append(dest.name)
        manifest["db_backup_method"] = "sqlite_file_copy"
        manifest["sqlite_filename"] = dest.name

    def _backup_postgres(self, backup_dir: Path, manifest: dict) -> None:
        """Shells out to pg_dump (custom format, so pg_restore can target
        a single database or table selectively later). Requires pg_dump
        on PATH and network access to POSTGRES_HOST — neither is
        guaranteed in every environment this command might run in, so a
        failure here is raised, not swallowed."""
        import os
        import subprocess

        db = settings.DATABASES["default"]
        dest = backup_dir / "db.dump"
        env = os.environ.copy()
        env["PGPASSWORD"] = db["PASSWORD"]
        cmd = [
            "pg_dump",
            "-h", str(db["HOST"]),
            "-p", str(db.get("PORT") or 5432),
            "-U", str(db["USER"]),
            "-Fc",
            "-f", str(dest),
            str(db["NAME"]),
        ]
        try:
            # cmd itself carries no secret (password goes via PGPASSWORD env,
            # not argv) — safe to let this raise/log as-is.
            subprocess.run(cmd, env=env, check=True, capture_output=True, timeout=1800, text=True)
        except FileNotFoundError:
            self.stderr.write(self.style.ERROR("pg_dump not found on PATH — install the PostgreSQL client tools."))
            raise SystemExit(1)
        except subprocess.CalledProcessError as exc:
            self.stderr.write(self.style.ERROR(f"pg_dump failed (exit {exc.returncode}): {exc.stderr[:500]}"))
            raise SystemExit(1)
        manifest["files"].append(dest.name)
        manifest["db_backup_method"] = "pg_dump_custom_format"

    def _backup_model_artifacts(self, backup_dir: Path, manifest: dict) -> None:
        cache_dir = Path(settings.CACHE_DIR)
        for pkl in sorted(cache_dir.glob("*.pkl")):
            dest = backup_dir / pkl.name
            shutil.copy2(pkl, dest)
            manifest["files"].append(dest.name)
        autosync_state = cache_dir / "autosync_state.txt"
        if autosync_state.exists():
            shutil.copy2(autosync_state, backup_dir / autosync_state.name)
            manifest["files"].append(autosync_state.name)

    def _snapshot_row_counts(self, manifest: dict) -> None:
        try:
            from market.models import AnalysisResult, PriceHistory, Stock

            manifest["row_counts"] = {
                "Stock": Stock.objects.count(),
                "PriceHistory": PriceHistory.objects.count(),
                "AnalysisResult": AnalysisResult.objects.count(),
            }
        except Exception as exc:
            manifest["row_counts_error"] = str(exc)[:300]
