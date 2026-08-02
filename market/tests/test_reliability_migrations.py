"""Migration consistency: the models added for the ML Reliability Monitor
must have a migration already generated for them — this is what CI's
`makemigrations --check` guards against silently drifting."""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase


class MigrationConsistencyTests(TestCase):
    def test_no_missing_migrations(self):
        out = StringIO()
        try:
            call_command("makemigrations", "--check", "--dry-run", stdout=out, stderr=out)
        except SystemExit as exc:
            self.fail(f"makemigrations --check found un-migrated model changes:\n{out.getvalue()}\nexit={exc.code}")
