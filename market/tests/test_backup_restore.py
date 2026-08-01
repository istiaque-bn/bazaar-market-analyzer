"""
Phase 9 — backup_bazaar / verify_backup.

Two separate concerns, tested separately:

1. backup_bazaar writes the expected files/checksums/manifest shape —
   tested against a synthetic on-disk sqlite file via --sqlite-path
   (Django's test database is an in-memory sqlite the command's default
   path can't copy as a file, which is exactly why --sqlite-path exists).

2. verify_backup's actual verification logic (checksum mismatch, sqlite
   corruption, row-count mismatch) — tested against hand-built backup
   directories with row counts we fully control, so a mismatch is a
   deliberate, deterministic test condition rather than an artifact of
   whatever the live test database happens to contain.

Neither test touches the real db.sqlite3 or data/cache/*.pkl — see
market/tests/test_data_quality.py's module docstring for the project's
general stance on that. (A real, non-synthetic end-to-end run of both
commands against the actual repo database is documented in the Phase 9
report, not repeated here — this file is the fast, deterministic,
CI-safe coverage of the same code paths.)
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_fake_sqlite_db(path: Path, *, stocks: int, prices: int, analyses: int) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE market_stock (id INTEGER PRIMARY KEY, trading_code TEXT)")
    conn.execute("CREATE TABLE market_pricehistory (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE market_analysisresult (id INTEGER PRIMARY KEY)")
    conn.executemany("INSERT INTO market_stock (trading_code) VALUES (?)", [(f"S{i}",) for i in range(stocks)])
    conn.executemany("INSERT INTO market_pricehistory DEFAULT VALUES", [() for _ in range(prices)])
    conn.executemany("INSERT INTO market_analysisresult DEFAULT VALUES", [() for _ in range(analyses)])
    conn.commit()
    conn.close()


def _write_backup_dir(tmp: Path, *, stocks=3, prices=7, analyses=2, manifest_overrides=None, corrupt=False) -> Path:
    """Hand-builds a directory in exactly the shape backup_bazaar
    produces, with fully-known contents — the fixture verify_backup's
    own tests check against."""
    backup_dir = tmp / "20260101_000000"
    backup_dir.mkdir(parents=True)
    db_path = backup_dir / "db.sqlite3"
    _make_fake_sqlite_db(db_path, stocks=stocks, prices=prices, analyses=analyses)
    if corrupt:
        with open(db_path, "r+b") as f:
            f.seek(20)
            f.write(b"CORRUPTED")

    manifest = {
        "created_at": "2026-01-01T00:00:00+00:00",
        "db_engine": "django.db.backends.sqlite3",
        "db_backup_method": "sqlite_file_copy",
        "sqlite_filename": "db.sqlite3",
        "files": ["db.sqlite3"],
        "row_counts": {"Stock": stocks, "PriceHistory": prices, "AnalysisResult": analyses},
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)
    (backup_dir / "manifest.json").write_text(json.dumps(manifest))

    with open(backup_dir / "SHA256SUMS.txt", "w") as f:
        f.write(f"{_sha256(db_path)}  db.sqlite3\n")
    return backup_dir


class BackupBazaarCommandTests(TestCase):
    # TestCase (not SimpleTestCase): _snapshot_row_counts genuinely
    # queries Stock/PriceHistory/AnalysisResult via the ORM, which
    # SimpleTestCase forbids outright.
    def test_writes_db_copy_checksums_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src_db = tmp / "source.sqlite3"
            _make_fake_sqlite_db(src_db, stocks=3, prices=7, analyses=2)
            dest_root = tmp / "backups"
            out = StringIO()
            call_command("backup_bazaar", sqlite_path=str(src_db), dest=str(dest_root), stdout=out)

            dirs = list(dest_root.iterdir())
            self.assertEqual(len(dirs), 1)
            backup_dir = dirs[0]

            self.assertTrue((backup_dir / "source.sqlite3").exists())  # preserves the original filename
            self.assertTrue((backup_dir / "SHA256SUMS.txt").exists())
            manifest = json.loads((backup_dir / "manifest.json").read_text())
            self.assertEqual(manifest["db_backup_method"], "sqlite_file_copy")
            self.assertEqual(manifest["sqlite_filename"], "source.sqlite3")
            self.assertIn("Stock", manifest["row_counts"])

            # The copy's checksum must match what's recorded.
            sums = (backup_dir / "SHA256SUMS.txt").read_text()
            self.assertIn(_sha256(backup_dir / "source.sqlite3"), sums)

            self.assertIn("NOT yet verified", out.getvalue())

    def test_refuses_to_overwrite_an_existing_timestamp_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src_db = tmp / "source.sqlite3"
            _make_fake_sqlite_db(src_db, stocks=1, prices=1, analyses=1)
            dest_root = tmp / "backups"
            existing = dest_root / "20260101_000000"
            existing.mkdir(parents=True)
            with self.assertRaises(FileExistsError):
                from unittest import mock

                from django.utils import timezone as tz

                fixed_now = tz.datetime(2026, 1, 1, 0, 0, 0, tzinfo=tz.get_current_timezone())
                with mock.patch("django.utils.timezone.now", return_value=fixed_now):
                    call_command("backup_bazaar", sqlite_path=str(src_db), dest=str(dest_root), stdout=StringIO())

    def test_prune_keep_removes_only_older_backup_looking_dirs(self):
        from market.management.commands.backup_bazaar import _prune_old_backups

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            names = ["20260101_000000", "20260102_000000", "20260103_000000"]
            for name in names:
                d = root / name
                d.mkdir()
                (d / "manifest.json").write_text("{}")
                (d / "SHA256SUMS.txt").write_text("")
            # An unrelated directory a human left here must survive pruning.
            unrelated = root / "not_a_backup"
            unrelated.mkdir()

            out = StringIO()
            _prune_old_backups(root, keep=1, stdout=out)

            remaining = {d.name for d in root.iterdir()}
            self.assertEqual(remaining, {"20260103_000000", "not_a_backup"})


class VerifyBackupCommandTests(SimpleTestCase):
    def test_valid_backup_passes_every_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = _write_backup_dir(Path(tmp))
            out = StringIO()
            call_command("verify_backup", str(backup_dir), stdout=out)
            self.assertIn("RESTORE VERIFIED", out.getvalue())

    def test_never_writes_back_to_the_backup_dir_or_touches_real_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = _write_backup_dir(Path(tmp))
            before = {p.name: p.stat().st_mtime for p in backup_dir.iterdir()}
            call_command("verify_backup", str(backup_dir), stdout=StringIO())
            after = {p.name: p.stat().st_mtime for p in backup_dir.iterdir()}
            self.assertEqual(before, after)  # nothing in the backup dir itself was modified

    def test_file_corrupted_before_checksum_was_taken_fails_integrity_check(self):
        # _write_backup_dir computes the checksum *after* corrupting the
        # file, so sha256 matches (it would — the file transferred
        # correctly, it just wasn't a valid sqlite file to begin with).
        # PRAGMA integrity_check is what catches this case.
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = _write_backup_dir(Path(tmp), corrupt=True)
            out = StringIO()
            with self.assertRaises(SystemExit):
                call_command("verify_backup", str(backup_dir), stdout=out)
            self.assertIn("[FAIL] sqlite_integrity_check", out.getvalue())
            self.assertIn("RESTORE VERIFICATION FAILED", out.getvalue())

    def test_checksum_mismatch_after_backup_is_detected(self):
        # This time corrupt the file *after* SHA256SUMS.txt was written
        # (the realistic "bit rot / bad transfer since backup" scenario)
        # — sha256 must catch it even before any sqlite-level check runs.
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = _write_backup_dir(Path(tmp))
            with open(backup_dir / "db.sqlite3", "r+b") as f:
                f.seek(20)
                f.write(b"CORRUPTED")
            out = StringIO()
            with self.assertRaises(SystemExit):
                call_command("verify_backup", str(backup_dir), stdout=out)
            self.assertIn("[FAIL] sha256:db.sqlite3", out.getvalue())
            self.assertIn("RESTORE VERIFICATION FAILED", out.getvalue())

    def test_row_count_mismatch_against_manifest_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Manifest claims 999 PriceHistory rows; the actual restored
            # file only has 7 — this is exactly the "backup silently
            # missing data" scenario the row-count check exists to catch.
            backup_dir = _write_backup_dir(Path(tmp), manifest_overrides={"row_counts": {"Stock": 3, "PriceHistory": 999, "AnalysisResult": 2}})
            out = StringIO()
            with self.assertRaises(SystemExit):
                call_command("verify_backup", str(backup_dir), stdout=out)
            self.assertIn("[FAIL] row_count:PriceHistory", out.getvalue())

    def test_missing_sha256sums_file_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = _write_backup_dir(Path(tmp))
            (backup_dir / "SHA256SUMS.txt").unlink()
            out = StringIO()
            with self.assertRaises(SystemExit):
                call_command("verify_backup", str(backup_dir), stdout=out)
            self.assertIn("sha256sums_present", out.getvalue())

    def test_json_output_is_machine_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = _write_backup_dir(Path(tmp))
            out = StringIO()
            call_command("verify_backup", str(backup_dir), "--json", stdout=out)
            payload = json.loads(out.getvalue())
            self.assertTrue(payload["ok"])
            self.assertTrue(all(c["ok"] for c in payload["checks"]))
