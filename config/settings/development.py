"""
Local development settings. This is the default settings module (see
manage.py / config/wsgi.py / config/asgi.py / config/celery.py) so that
plain `python manage.py runserver` keeps working exactly as before —
nothing here changes existing local behavior.

Every relaxed/insecure choice below is intentional and scoped to local
HTTP development; production.py does the opposite of each one.
"""
import os
from urllib.parse import unquote, urlparse

from .base import *  # noqa: F401,F403

SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-bazaar-change-me")
# Keep development isolated from a shell-level production/release DEBUG flag.
# Use DEV_DEBUG=False only when deliberately testing a production-like local
# response; normal `manage.py runserver` must serve static assets.
DEBUG = os.getenv("DEV_DEBUG", "True").lower() in ("1", "true", "yes")
ALLOWED_HOSTS = [h.strip() for h in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]

# Local exception: the dev server runs over plain HTTP, so secure-only
# cookies/HSTS/SSL-redirect would just break login — these stay off here
# and are turned on unconditionally in production.py.
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0

# Local exception: lets the Vite/React-style dev tooling or a mobile
# simulator hit the API from any origin during development.
CORS_ALLOW_ALL_ORIGINS = True

# Local exception: base.py defaults ENABLE_CSE to False (the production-
# facing "DSE-only by default for new deployments" behavior), but this
# whole codebase — including `manage.py test`, which runs under this
# module by default — predates the exchange feature flag and exercises
# CSE broadly without expecting it to be off. Re-enable it here so local
# `runserver`/`manage.py test` keep behaving exactly as before unless a
# developer explicitly sets ENABLE_CSE=False in their own .env to test
# DSE-only mode locally.
ENABLE_DSE = os.getenv("ENABLE_DSE", "True").strip().lower() in ("1", "true", "yes")
ENABLE_CSE = os.getenv("ENABLE_CSE", "True").strip().lower() in ("1", "true", "yes")

# Opt-in local PostgreSQL snapshot support.  This keeps ordinary development
# on SQLite, while allowing a separately launched server to inspect a restored
# production backup without editing .env or replacing db.sqlite3.
_dev_postgres_url = os.getenv("DEV_POSTGRES_URL", "").strip()
if _dev_postgres_url:
    _parsed_dev_db = urlparse(_dev_postgres_url)
    if _parsed_dev_db.scheme not in {"postgres", "postgresql"} or not _parsed_dev_db.path:
        raise ValueError("DEV_POSTGRES_URL must be a PostgreSQL URL with a database name.")
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": unquote(_parsed_dev_db.path.lstrip("/")),
            "USER": unquote(_parsed_dev_db.username or ""),
            "PASSWORD": unquote(_parsed_dev_db.password or ""),
            "HOST": _parsed_dev_db.hostname or "127.0.0.1",
            "PORT": str(_parsed_dev_db.port or 5432),
        }
    }
