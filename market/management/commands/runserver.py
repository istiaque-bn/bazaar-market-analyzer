"""
Overrides `runserver` to run a pre-flight check (DB, migrations, Redis,
Celery worker/beat, full test suite) once per `manage.py runserver`
invocation, printing pass/fail per item and a final readiness banner
before handing off to Django's normal (staticfiles-aware) dev server.

Subclasses staticfiles' runserver (not the bare core one) since that's
the one actually active with "django.contrib.staticfiles" installed —
subclassing anything else would silently drop static file serving.

Only runs in the outer process: with the autoreloader on, Django re-execs
a fresh child (RUN_MAIN=true) on every restart, including every autosave
during dev — running the full suite on each of those would make every
file save take 30+ seconds. Checking for RUN_MAIN keeps this to exactly
once per terminal invocation. Set SKIP_PREFLIGHT=1 to bypass entirely
(e.g. for a fast inner loop while iterating).
"""
from __future__ import annotations

import os
import subprocess
import time

from django.conf import settings
from django.contrib.staticfiles.management.commands.runserver import Command as StaticfilesRunserverCommand
from django.core.management import call_command
from django.db import connection

ENSURE_SERVICES_SCRIPT = str(settings.BASE_DIR / ".claude" / "scripts" / "ensure_bazaar_services.sh")
WORKER_PIDFILE = "/tmp/bazaar-services/worker.pid"
BEAT_PIDFILE = "/tmp/bazaar-services/beat.pid"


class Command(StaticfilesRunserverCommand):
    def run(self, **options):
        if os.environ.get("RUN_MAIN") != "true" and not os.environ.get("SKIP_PREFLIGHT"):
            self._preflight()
        super().run(**options)

    def _status(self, label, ok, detail=""):
        mark = self.style.SUCCESS("OK") if ok else self.style.ERROR("FAILED")
        suffix = f" — {detail}" if detail and not ok else ""
        self.stdout.write(f"  [{mark}] {label}{suffix}")
        return ok

    def _pid_alive(self, pidfile_path):
        try:
            with open(pidfile_path) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            return True
        except (OSError, ValueError, FileNotFoundError):
            return False

    def _preflight(self):
        self.stdout.write("")
        self.stdout.write("Running pre-flight checks...")
        results = []

        try:
            connection.ensure_connection()
            results.append(self._status("Database connection", True))
        except Exception as exc:
            results.append(self._status("Database connection", False, str(exc)))

        try:
            from django.db.migrations.executor import MigrationExecutor

            executor = MigrationExecutor(connection)
            plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
            results.append(self._status("Migrations up to date", not plan, f"{len(plan)} unapplied" if plan else ""))
        except Exception as exc:
            results.append(self._status("Migrations up to date", False, str(exc)))

        try:
            import redis

            redis.Redis.from_url(settings.CELERY_BROKER_URL, socket_connect_timeout=3).ping()
            results.append(self._status("Redis (Celery broker)", True))
        except Exception as exc:
            results.append(self._status("Redis (Celery broker)", False, str(exc)))

        try:
            subprocess.run(["bash", ENSURE_SERVICES_SCRIPT], check=False, timeout=20, capture_output=True, text=True)
        except Exception:
            pass
        results.append(self._status("Celery worker", self._pid_alive(WORKER_PIDFILE)))
        results.append(self._status("Celery beat", self._pid_alive(BEAT_PIDFILE)))

        self.stdout.write("  Running test suite...")
        t0 = time.time()
        try:
            call_command("test", verbosity=0)
            tests_ok = True
        except SystemExit:
            tests_ok = False
        elapsed = time.time() - t0
        results.append(self._status(f"Test suite ({elapsed:.1f}s)", tests_ok))

        self.stdout.write("")
        if all(results):
            self.stdout.write(self.style.SUCCESS("All tests and services running smoothly — the app is ready."))
        else:
            self.stdout.write(self.style.WARNING("Some checks failed (see above) — starting the server anyway."))
        self.stdout.write("")
