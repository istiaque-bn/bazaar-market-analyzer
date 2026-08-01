"""
Production settings. Set DJANGO_SETTINGS_MODULE=config.settings.production
explicitly to use this — it is never the default (see manage.py), and it
never runs itself; a human/deploy pipeline opts into it.

Design rule for this whole module: missing or placeholder configuration
must raise ImproperlyConfigured at import time (i.e. the process refuses
to start) rather than silently falling back to an insecure or
development-shaped default. See _require_env() below.
"""
import os

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401,F403


def _require_env(name: str, *, doc: str = "") -> str:
    value = os.getenv(name)
    if not value:
        hint = f" ({doc})" if doc else ""
        raise ImproperlyConfigured(f"{name} is required in production{hint}. See .env.production.example.")
    return value


# --- Core: SECRET_KEY / DEBUG / ALLOWED_HOSTS / CSRF_TRUSTED_ORIGINS ----
SECRET_KEY = _require_env(
    "SECRET_KEY", doc='generate with: python -c "import secrets; print(secrets.token_urlsafe(64))"'
)
_INSECURE_SECRET_KEYS = {
    "dev-insecure-bazaar-change-me",
    "change-me-in-production",
    "test-only-secret-key-not-for-any-real-use",
    "unset-base-settings-should-not-be-used-directly",
}
if SECRET_KEY in _INSECURE_SECRET_KEYS or len(SECRET_KEY) < 32:
    raise ImproperlyConfigured(
        "SECRET_KEY is missing, a known placeholder, or shorter than 32 characters — refusing to start in "
        "production with a weak key. Generate a strong random value (see .env.production.example)."
    )

DEBUG = False  # not configurable via env in production — always off, no exceptions

_allowed_hosts_raw = _require_env("ALLOWED_HOSTS", doc="comma-separated hostnames, e.g. bazaar.example.com")
ALLOWED_HOSTS = [h.strip() for h in _allowed_hosts_raw.split(",") if h.strip()]
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured("ALLOWED_HOSTS must contain at least one hostname in production.")

_csrf_trusted_raw = _require_env(
    "CSRF_TRUSTED_ORIGINS", doc="comma-separated full origins with scheme, e.g. https://bazaar.example.com"
)
CSRF_TRUSTED_ORIGINS = [o.strip() for o in _csrf_trusted_raw.split(",") if o.strip()]
if not CSRF_TRUSTED_ORIGINS:
    raise ImproperlyConfigured("CSRF_TRUSTED_ORIGINS must contain at least one origin in production.")
for _origin in CSRF_TRUSTED_ORIGINS:
    if not (_origin.startswith("https://") or _origin.startswith("http://")):
        raise ImproperlyConfigured(f"CSRF_TRUSTED_ORIGINS entry {_origin!r} must include a scheme (https://...).")

# --- HTTPS / proxy / HSTS / secure cookies / security headers ----------
# Assumes a reverse proxy (nginx, an ALB, Cloudflare, etc.) terminates
# TLS and forwards `X-Forwarded-Proto` — the standard shape for gunicorn
# behind a load balancer. If Django/gunicorn terminates TLS directly
# instead, SECURE_PROXY_SSL_HEADER must be removed (trusting that header
# from a client that talks to Django directly is a spoofable-scheme
# vulnerability) and TLS termination handled at that layer instead.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = os.getenv("SECURE_SSL_REDIRECT", "True").lower() in ("1", "true", "yes")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "31536000"))  # 1 year, matches Django's `check --deploy` recommendation
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# --- CORS: explicit allow-list only — the permissive `CORS_ALLOW_ALL_ORIGINS`
# used in development.py would let any website read the API from a
# logged-in user's browser; production must name its origins.
CORS_ALLOW_ALL_ORIGINS = False
_cors_origins_raw = os.getenv("CORS_ALLOWED_ORIGINS", "")
CORS_ALLOWED_ORIGINS = [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]

# --- Database: PostgreSQL via env vars ----------------------------------
# See README "Deployment" / docs/DEPLOYMENT.md for the documented,
# manual SQLite → PostgreSQL migration procedure. Nothing in this
# settings module (or anywhere else) runs that migration automatically —
# it is a human-triggered, backed-up, verified operation.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _require_env("POSTGRES_DB"),
        "USER": _require_env("POSTGRES_USER"),
        "PASSWORD": _require_env("POSTGRES_PASSWORD"),
        "HOST": _require_env("POSTGRES_HOST"),
        "PORT": os.getenv("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": int(os.getenv("POSTGRES_CONN_MAX_AGE", "60")),
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {"sslmode": os.getenv("POSTGRES_SSLMODE", "require")},
    }
}

# --- Celery/Redis: require an explicit broker; refuse plaintext by default
CELERY_BROKER_URL = _require_env(
    "CELERY_BROKER_URL", doc="use rediss:// with auth for TLS, e.g. rediss://:password@host:6380/0"
)
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", CELERY_BROKER_URL)
if CELERY_BROKER_URL.startswith("redis://") and os.getenv("CELERY_BROKER_ALLOW_PLAINTEXT", "False").lower() not in (
    "1",
    "true",
    "yes",
):
    raise ImproperlyConfigured(
        "CELERY_BROKER_URL uses plaintext redis:// in production. Use rediss:// (TLS) with authentication, or "
        "set CELERY_BROKER_ALLOW_PLAINTEXT=True to explicitly accept plaintext (e.g. broker reachable only over "
        "a private VPC network you control)."
    )

# --- Static & media ------------------------------------------------------
# WhiteNoise serves compressed, cache-busted static files directly from
# the app process — no separate static-file server or external object
# storage required, so this doesn't introduce a new external service.
# Requires `python manage.py collectstatic` at deploy time (see README).
_security_index = MIDDLEWARE.index("django.middleware.security.SecurityMiddleware")
MIDDLEWARE = MIDDLEWARE[: _security_index + 1] + ["whitenoise.middleware.WhiteNoiseMiddleware"] + MIDDLEWARE[_security_index + 1 :]

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# --- Logging: stdout/stderr only (12-factor) — the process supervisor /
# platform log collector owns rotation, retention and shipping. Same
# correlation-id filter and handler wiring as base.py; only the
# formatter changes, to structured JSON (one object per line) so a log
# aggregator can parse fields instead of scraping free text.
LOGGING["formatters"]["console"] = {
    "()": "config.logging_utils.RedactingJsonFormatter",
}
LOGGING["root"]["level"] = os.getenv("DJANGO_LOG_LEVEL", "INFO")
