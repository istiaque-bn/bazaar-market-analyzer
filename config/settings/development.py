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
