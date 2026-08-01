"""
Phase 8: configuration tests for config/settings/{development,test,production}.py.

Each settings module runs its validation/branching logic at *import*
time (module-level code, not inside a function), so the only reliable
way to test "does importing this module with env X raise/behave like Y"
is a fresh subprocess per case — importing it in-process would either
reuse an already-configured django.conf.settings from this same test
run, or leave sys.modules poisoned for subsequent cases. Every subprocess
gets an explicit, minimal env (never the real developer .env file's
values) so these tests are deterministic regardless of local secrets.
"""
import json
import subprocess
import sys
from pathlib import Path

from django.test import SimpleTestCase

BASE_DIR = Path(__file__).resolve().parent.parent.parent

VALID_PROD_ENV = {
    "DJANGO_SETTINGS_MODULE": "config.settings.production",
    # Fixed (not regenerated per run) so tests are deterministic; high
    # entropy so it also clears Django's own security.W009 deploy check
    # (which additionally requires >=50 chars and >=5 unique characters —
    # a repeated-character string like "a" * 64 passes our own length
    # gate but fails Django's).
    "SECRET_KEY": "LOMVgTxwoNibVHtDWZ5Y6BBNQLG3vZlky4hwZSVD1rjCbOUftaphJRPSmzQDGmP0",
    "ALLOWED_HOSTS": "bazaar.example.com",
    "CSRF_TRUSTED_ORIGINS": "https://bazaar.example.com",
    "POSTGRES_DB": "bazaar",
    "POSTGRES_USER": "bazaar",
    "POSTGRES_PASSWORD": "x",
    "POSTGRES_HOST": "localhost",
    "CELERY_BROKER_URL": "rediss://:pw@localhost:6380/0",
}

READ_SETTINGS_SCRIPT = """
import json, os
os.environ["DJANGO_SETTINGS_MODULE"] = "{module}"
from django.conf import settings
print(json.dumps({{
    "DEBUG": settings.DEBUG,
    "SECRET_KEY": settings.SECRET_KEY,
    "ALLOWED_HOSTS": settings.ALLOWED_HOSTS,
    "SESSION_COOKIE_SECURE": settings.SESSION_COOKIE_SECURE,
    "CSRF_COOKIE_SECURE": settings.CSRF_COOKIE_SECURE,
    "SECURE_SSL_REDIRECT": settings.SECURE_SSL_REDIRECT,
    "SECURE_HSTS_SECONDS": settings.SECURE_HSTS_SECONDS,
    "CORS_ALLOW_ALL_ORIGINS": settings.CORS_ALLOW_ALL_ORIGINS,
    "DB_ENGINE": settings.DATABASES["default"]["ENGINE"],
    "MIDDLEWARE": list(settings.MIDDLEWARE),
    "PASSWORD_HASHERS": list(settings.PASSWORD_HASHERS),
    "STATICFILES_BACKEND": settings.STORAGES["staticfiles"]["BACKEND"],
}}))
"""


def _run(module: str, env_overrides: dict) -> subprocess.CompletedProcess:
    env = {"PATH": __import__("os").environ.get("PATH", "")}
    env.update(env_overrides)
    script = READ_SETTINGS_SCRIPT.format(module=module)
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(BASE_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _run_manage_check_deploy(env_overrides: dict) -> subprocess.CompletedProcess:
    env = {"PATH": __import__("os").environ.get("PATH", "")}
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "manage.py", "check", "--deploy"],
        cwd=str(BASE_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


class DevelopmentSettingsTests(SimpleTestCase):
    def test_imports_cleanly_and_has_documented_local_http_exceptions(self):
        result = _run(
            "config.settings.development",
            {"SECRET_KEY": "dev-key", "DEBUG": "True", "ALLOWED_HOSTS": "localhost"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data["DEBUG"])
        # Local-HTTP exceptions are unconditional in development.py, not
        # env-controlled — see docs/DEPLOYMENT.md.
        self.assertFalse(data["SESSION_COOKIE_SECURE"])
        self.assertFalse(data["CSRF_COOKIE_SECURE"])
        self.assertFalse(data["SECURE_SSL_REDIRECT"])
        self.assertEqual(data["SECURE_HSTS_SECONDS"], 0)
        self.assertTrue(data["CORS_ALLOW_ALL_ORIGINS"])
        self.assertEqual(data["DB_ENGINE"], "django.db.backends.sqlite3")

    def test_debug_follows_env_var(self):
        result = _run("config.settings.development", {"DEBUG": "False"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["DEBUG"])


class TestSettingsTests(SimpleTestCase):
    def test_imports_cleanly_with_test_friendly_overrides(self):
        result = _run("config.settings.test", {})
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertFalse(data["DEBUG"])
        self.assertIn("testserver", data["ALLOWED_HOSTS"])
        self.assertIn("django.contrib.auth.hashers.MD5PasswordHasher", data["PASSWORD_HASHERS"])
        # Same local-HTTP exception as development.py, same reason (test
        # client has no TLS).
        self.assertFalse(data["SESSION_COOKIE_SECURE"])


class ProductionSettingsFailFastTests(SimpleTestCase):
    """Each of these omits exactly one required var (set to "" so a
    developer's real .env can't silently supply it — see module
    docstring) and asserts the process refuses to start."""

    def _assert_refuses_to_start(self, env_overrides: dict, expected_in_message: str):
        env = dict(VALID_PROD_ENV)
        env.update(env_overrides)
        result = _run("config.settings.production", env)
        self.assertNotEqual(result.returncode, 0, "expected import to fail but it succeeded")
        self.assertIn("ImproperlyConfigured", result.stderr)
        self.assertIn(expected_in_message, result.stderr)

    def test_missing_secret_key(self):
        self._assert_refuses_to_start({"SECRET_KEY": ""}, "SECRET_KEY")

    def test_placeholder_secret_key(self):
        self._assert_refuses_to_start({"SECRET_KEY": "dev-insecure-bazaar-change-me"}, "SECRET_KEY")

    def test_short_secret_key(self):
        self._assert_refuses_to_start({"SECRET_KEY": "short"}, "SECRET_KEY")

    def test_missing_allowed_hosts(self):
        self._assert_refuses_to_start({"ALLOWED_HOSTS": ""}, "ALLOWED_HOSTS")

    def test_missing_csrf_trusted_origins(self):
        self._assert_refuses_to_start({"CSRF_TRUSTED_ORIGINS": ""}, "CSRF_TRUSTED_ORIGINS")

    def test_csrf_trusted_origin_without_scheme(self):
        self._assert_refuses_to_start({"CSRF_TRUSTED_ORIGINS": "bazaar.example.com"}, "CSRF_TRUSTED_ORIGINS")

    def test_missing_postgres_db(self):
        self._assert_refuses_to_start({"POSTGRES_DB": ""}, "POSTGRES_DB")

    def test_missing_postgres_password(self):
        self._assert_refuses_to_start({"POSTGRES_PASSWORD": ""}, "POSTGRES_PASSWORD")

    def test_missing_celery_broker_url(self):
        self._assert_refuses_to_start({"CELERY_BROKER_URL": ""}, "CELERY_BROKER_URL")

    def test_plaintext_redis_broker_refused_by_default(self):
        self._assert_refuses_to_start({"CELERY_BROKER_URL": "redis://localhost:6379/0"}, "plaintext redis://")

    def test_plaintext_redis_broker_allowed_with_explicit_opt_out(self):
        env = dict(VALID_PROD_ENV)
        env["CELERY_BROKER_URL"] = "redis://localhost:6379/0"
        env["CELERY_BROKER_ALLOW_PLAINTEXT"] = "True"
        result = _run("config.settings.production", env)
        self.assertEqual(result.returncode, 0, result.stderr)


class ProductionSettingsValidConfigTests(SimpleTestCase):
    def test_imports_and_hardens_correctly_with_full_valid_env(self):
        result = _run("config.settings.production", VALID_PROD_ENV)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertFalse(data["DEBUG"])
        self.assertTrue(data["SESSION_COOKIE_SECURE"])
        self.assertTrue(data["CSRF_COOKIE_SECURE"])
        self.assertTrue(data["SECURE_SSL_REDIRECT"])
        self.assertEqual(data["SECURE_HSTS_SECONDS"], 31536000)
        self.assertFalse(data["CORS_ALLOW_ALL_ORIGINS"])
        self.assertEqual(data["DB_ENGINE"], "django.db.backends.postgresql")
        self.assertIn("whitenoise.middleware.WhiteNoiseMiddleware", data["MIDDLEWARE"])
        self.assertEqual(
            data["STATICFILES_BACKEND"],
            "whitenoise.storage.CompressedManifestStaticFilesStorage",
        )

    def test_manage_check_deploy_reports_no_issues(self):
        result = _run_manage_check_deploy(VALID_PROD_ENV)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("System check identified no issues", result.stderr + result.stdout)
