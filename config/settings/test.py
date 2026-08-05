"""
Settings for automated test runs (CI or `--settings=config.settings.test`
locally). Not used by default — `manage.py test` normally runs under
development.py (see manage.py), which is already safe for tests and is
what this whole project's test suite has been validated against. This
module exists for Phase 8's "environment-specific settings" requirement
and for CI setups that want a settings module that never reads a local
developer's .env secrets.

Deliberately conservative: it changes only things that are safe by
construction (fixed dummy secret, fast password hashing) and leaves
Celery/autosync/email behavior alone. An earlier version of this module
also forced CELERY_TASK_ALWAYS_EAGER / CELERY_TASK_EAGER_PROPAGATES and
turned the AUTO_* flags off, on the theory that tests shouldn't touch
background-task machinery — but several existing tests
(market/tests/test_task_idempotency.py) exercise that machinery
directly and assume Celery's own default eager-retry behavior and
AUTO_MARKET_SYNC=True; forcing those settings silently broke them
(caught by running the full suite under this module before shipping).
Don't reintroduce those overrides without re-running the full suite
under this settings module.
"""
from .base import *  # noqa: F401,F403

SECRET_KEY = "test-only-secret-key-not-for-any-real-use"
DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]

# Tests run over Django's test client (fake http host, no TLS) — same
# local exception as development.py, for the same reason.
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0

# Password hashing is deliberately slow in production; that cost buys
# nothing in tests and adds up across hundreds of user-creation calls.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Deliberately hardcoded (not env-driven, unlike base.py's ENABLE_CSE
# default) so this settings module's test runs are 100% deterministic
# regardless of whatever a developer's or CI runner's local .env happens
# to set — the pre-existing suite exercises CSE broadly and must keep
# passing under this module exactly like it does under development.py's
# own explicit re-enable. Tests exercising the disabled-CSE path use
# @override_settings(ENABLE_CSE=False) explicitly rather than relying on
# either settings module's ambient default (see market/tests/test_exchange_config.py).
ENABLE_DSE = True
ENABLE_CSE = True
