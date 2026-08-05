"""
Local development settings. This is the default settings module (see
manage.py / config/wsgi.py / config/asgi.py / config/celery.py) so that
plain `python manage.py runserver` keeps working exactly as before —
nothing here changes existing local behavior.

Every relaxed/insecure choice below is intentional and scoped to local
HTTP development; production.py does the opposite of each one.
"""
import os

from .base import *  # noqa: F401,F403

SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-bazaar-change-me")
DEBUG = os.getenv("DEBUG", "True").lower() in ("1", "true", "yes")
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
