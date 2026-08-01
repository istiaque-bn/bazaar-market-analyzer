"""Phase 9: perform an ISOLATED test restore of a backup directory and
verify it — this is the only thing that lets anyone honestly say "this
backup works". It never writes to the real db.sqlite3, data/cache/*.pkl,
or any file backup_bazaar.py produced; everything happens in a fresh
scratch directory (a tempfile.mkdtemp() by default) that's the sole
target of every restore/check performed here.

Checks performed, in order (each is independent — one failing doesn't
skip the rest, so a single run reports everything wrong at once):
  1. SHA256SUMS.txt: every listed file's hash matches its current bytes
     (catches silent corruption/truncation since the backup was taken).
  2. manifest.json parses and names a supported db_backup_method.
  3. sqlite: the *restored copy* passes `PRAGMA integrity_check` (catches
     a corrupt/partial sqlite file that still has plausible-looking bytes).
  4. sqlite: row counts in the restored copy match the manifest's
     snapshot at backup time, via a direct read of the restored file with
     the stdlib sqlite3 module — deliberately not Django's ORM/connection
     machinery, so this can never accidentally touch the app's live
     `default` database connection.
  postgresql backups get 1-2 plus a pg_restore --list structural check
  (no live database to restore into in every environment, so row-count
  verification there needs a real target instance — see docs/RUNBOOKS.md).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from django.core.management.base import BaseCommand


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


TABLE_BY_MODEL = {
    "Stock": "market_stock",
    "PriceHistory": "market_pricehistory",
    "AnalysisResult": "market_analysisresult",
}


class Command(BaseCommand):
    help = "Isolated test-restore verification of a backup_bazaar.py backup directory. Never touches the real database or model files."

    def add_arguments(self, parser):
        parser.add_argument("backup_dir")
        parser.add_argument("--workdir", default=None, help="Isolated scratch dir (default: a fresh tempfile.mkdtemp())")
        parser.add_argument("--keep-workdir", action="store_true", help="Don't delete the scratch dir afterward (for inspection)")
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **options):
        backup_dir = Path(options["backup_dir"]).resolve()
        checks: list[dict] = []

        checks.extend(self._verify_checksums(backup_dir))

        manifest_path = backup_dir / "manifest.json"
        manifest = None
        if not manifest_path.exists():
            checks.append({"check": "manifest_present", "ok": False, "detail": "manifest.json missing"})
        else:
            try:
                manifest = json.loads(manifest_path.read_text())
                checks.append({"check": "manifest_parses", "ok": True})
            except Exception as exc:
                checks.append({"check": "manifest_parses", "ok": False, "detail": str(exc)[:200]})

        workdir = Path(options["workdir"] or tempfile.mkdtemp(prefix="bazaar_restore_verify_"))
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            if manifest is not None:
                method = manifest.get("db_backup_method")
                if method == "sqlite_file_copy":
                    checks.extend(self._verify_sqlite_restore(backup_dir, manifest, workdir))
                elif method == "pg_dump_custom_format":
                    checks.extend(self._verify_postgres_dump_structure(backup_dir, manifest))
                else:
                    checks.append({"check": "known_backup_method", "ok": False, "detail": f"unrecognized: {method}"})
        finally:
            if not options["keep_workdir"]:
                shutil.rmtree(workdir, ignore_errors=True)

        all_ok = bool(checks) and all(c["ok"] for c in checks)

        if options["json"]:
            self.stdout.write(json.dumps({"backup_dir": str(backup_dir), "ok": all_ok, "checks": checks}, indent=2, default=str))
        else:
            for c in checks:
                style = self.style.SUCCESS if c["ok"] else self.style.ERROR
                line = f"[{'OK' if c['ok'] else 'FAIL'}] {c['check']}"
                if c.get("detail"):
                    line += f" — {c['detail']}"
                self.stdout.write(style(line))
            if all_ok:
                self.stdout.write(self.style.SUCCESS("RESTORE VERIFIED"))
            else:
                self.stdout.write(self.style.ERROR("RESTORE VERIFICATION FAILED"))

        if not all_ok:
            raise SystemExit(1)

    def _verify_checksums(self, backup_dir: Path) -> list[dict]:
        sums_path = backup_dir / "SHA256SUMS.txt"
        if not sums_path.exists():
            return [{"check": "sha256sums_present", "ok": False, "detail": "SHA256SUMS.txt missing"}]
        out = []
        for line in sums_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            digest, _, name = line.partition("  ")
            target = backup_dir / name
            if not target.exists():
                out.append({"check": f"sha256:{name}", "ok": False, "detail": "file missing"})
                continue
            actual = _sha256_file(target)
            out.append({"check": f"sha256:{name}", "ok": actual == digest})
        return out

    def _verify_sqlite_restore(self, backup_dir: Path, manifest: dict, workdir: Path) -> list[dict]:
        checks = []
        sqlite_name = manifest.get("sqlite_filename")
        if not sqlite_name or not (backup_dir / sqlite_name).exists():
            return [{"check": "sqlite_backup_file_present", "ok": False, "detail": sqlite_name or "not recorded in manifest"}]

        restored_path = workdir / "restored.sqlite3"
        shutil.copy2(backup_dir / sqlite_name, restored_path)  # the actual "restore" step — isolated copy, nothing else touched

        conn = sqlite3.connect(str(restored_path))
        try:
            try:
                result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            except sqlite3.DatabaseError as exc:
                # A corrupted header (not just corrupted page data further
                # in) fails here instead of returning a PRAGMA result —
                # still just "not a working backup", not a crash.
                checks.append({"check": "sqlite_integrity_check", "ok": False, "detail": str(exc)[:200]})
                checks.append(
                    {
                        "check": "row_counts",
                        "ok": False,
                        "detail": "skipped — database could not be opened",
                    }
                )
                return checks
            checks.append({"check": "sqlite_integrity_check", "ok": result == "ok", "detail": result})

            for model_name, table in TABLE_BY_MODEL.items():
                expected = (manifest.get("row_counts") or {}).get(model_name)
                try:
                    actual = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except sqlite3.DatabaseError as exc:
                    checks.append({"check": f"row_count:{model_name}", "ok": False, "detail": str(exc)[:200]})
                    continue
                ok = expected is None or actual == expected
                checks.append(
                    {
                        "check": f"row_count:{model_name}",
                        "ok": ok,
                        "detail": f"restored={actual} manifest={expected}",
                    }
                )
        finally:
            conn.close()
        return checks

    def _verify_postgres_dump_structure(self, backup_dir: Path, manifest: dict) -> list[dict]:
        """Structural-only check — lists the dump's table of contents via
        `pg_restore --list` without touching any live database. This is
        NOT the same guarantee as the sqlite path's real restore + row
        count comparison: it proves the dump file is well-formed, not
        that a fresh database restored from it matches expected data.
        Full verification needs a real (ideally disposable) Postgres
        instance — see docs/RUNBOOKS.md."""
        import subprocess

        dump_name = next((f for f in manifest.get("files", []) if f.endswith(".dump")), None)
        if not dump_name:
            return [{"check": "postgres_dump_file_present", "ok": False, "detail": "no .dump file in manifest"}]
        dump_path = backup_dir / dump_name
        try:
            result = subprocess.run(
                ["pg_restore", "--list", str(dump_path)], capture_output=True, text=True, timeout=120, check=False
            )
        except FileNotFoundError:
            return [
                {
                    "check": "postgres_dump_structure",
                    "ok": False,
                    "detail": "pg_restore not found on PATH — cannot even structurally verify this dump here",
                }
            ]
        ok = result.returncode == 0 and bool(result.stdout.strip())
        return [
            {
                "check": "postgres_dump_structure",
                "ok": ok,
                "detail": "pg_restore --list succeeded (structural check only — not a live restore)" if ok else result.stderr[:300],
            }
        ]
