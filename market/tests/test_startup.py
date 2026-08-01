import threading

from django.apps import apps
from django.test import TestCase


class AppStartupTests(TestCase):
    """AppConfig.ready() must not run DB queries or start daemon threads —
    background work is entirely Celery tasks now (market/tasks.py +
    config/celery.py's beat_schedule), not a runserver-only thread that
    silently doesn't exist under a real multi-process deployment."""

    def test_ready_runs_no_database_queries(self):
        config = apps.get_app_config("market")
        with self.assertNumQueries(0):
            config.ready()

    def test_ready_starts_no_threads(self):
        config = apps.get_app_config("market")
        before = threading.active_count()
        before_names = {t.name for t in threading.enumerate()}
        config.ready()
        after = threading.active_count()
        after_names = {t.name for t in threading.enumerate()}
        self.assertEqual(before, after)
        self.assertEqual(before_names, after_names)

    def test_no_thread_starter_functions_remain(self):
        """Regression guard: the old thread-launching entry points must be
        gone, not just uncalled — proves they can't be reintroduced by a
        stray call elsewhere without also re-adding the function."""
        from market.services import autosync, daily_append

        self.assertFalse(hasattr(autosync, "start_autosync_thread"))
        self.assertFalse(hasattr(daily_append, "start_daily_append_thread"))
