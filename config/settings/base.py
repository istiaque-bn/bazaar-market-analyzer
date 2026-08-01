"""
Bazaar — Bangladesh stock market analysis platform (DSE + CSE).

Shared settings for every environment. Nothing here should assume it's
running locally or in production — environment-specific values (DEBUG,
SECRET_KEY strength, cookie security, database engine, static file
serving) live in development.py / test.py / production.py, which all
start with `from .base import *`.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Project local time = Bangladesh market time (override with TIME_ZONE in .env)
TIME_ZONE = os.getenv("TIME_ZONE", "Asia/Dhaka").strip() or "Asia/Dhaka"
os.environ["TZ"] = TIME_ZONE
try:
    import time as _time

    if hasattr(_time, "tzset"):
        _time.tzset()
except Exception:
    pass

# Every environment module is expected to set SECRET_KEY, DEBUG and
# ALLOWED_HOSTS explicitly before/after importing this module — these are
# just a safe, inert fallback so `base.py` never accidentally serves
# traffic if it's ever loaded directly.
SECRET_KEY = "unset-base-settings-should-not-be-used-directly"
DEBUG = False
ALLOWED_HOSTS: list[str] = []

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "corsheaders",
    "django_celery_beat",
    "accounts.apps.AccountsConfig",
    "market.apps.MarketConfig",
    "notifications",
    "api",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Early so every subsequent middleware/view's log lines are already
    # tagged with a request_id (see config/logging_utils.py).
    "market.middleware.RequestIDMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "market.context_processors.market_nav",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# SQLite by default — production.py replaces this with PostgreSQL.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": {
            "timeout": 60,  # wait up to 60s if another thread holds the lock
        },
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
# TIME_ZONE set above from env (default Asia/Dhaka)
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "home"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticatedOrReadOnly",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "60/min",
        "user": "120/min",
        "register": "5/hour",
        "login": "10/min",
        "predict": "30/min",
    },
}

# Rate limit for the public, unauthenticated /ticker.json view (not a DRF
# endpoint, so it isn't covered by REST_FRAMEWORK's throttle rates above).
TICKER_JSON_RATE_LIMIT = (60, 60)  # (max requests, per seconds) per client IP

# CORS: base stays closed by default; development.py opens it for local
# tooling, production.py takes an explicit allow-list from the env.
CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGINS: list[str] = []

# --- Celery / Redis ---------------------------------------------------
# Base timeouts/worker settings apply everywhere; production.py requires
# the broker URL to be set explicitly (no localhost fallback) and
# documents the TLS/auth expectation (rediss://user:pass@host:port/db).
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", CELERY_BROKER_URL)
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
# Nothing in this app ever reads a task's AsyncResult — every task's
# outcome is tracked independently via market.models.TaskRun
# (@record_task_run). Without this, every scheduled send from Celery
# beat opens a Redis pub/sub "result consumer" connection it never
# needed; that connection has proven unstable inside Docker specifically
# (repeated "Connection to Redis lost" until beat's retry budget is
# exhausted and it stops scheduling entirely). Doesn't affect
# `.apply()`-based eager calls in tests (e.g. test_task_idempotency.py),
# which return their result in-process regardless of this setting.
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_ALWAYS_EAGER = os.getenv("CELERY_TASK_ALWAYS_EAGER", "False").lower() in ("1", "true", "yes")

# Connection/worker hardening — sane defaults everywhere, overridable via
# env in any environment. Individual tasks may set a tighter per-task
# time_limit/soft_time_limit (see notifications/tasks.py); these are the
# fallback ceiling for tasks that don't.
CELERY_BROKER_CONNECTION_TIMEOUT = int(os.getenv("CELERY_BROKER_CONNECTION_TIMEOUT", "10"))
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BROKER_TRANSPORT_OPTIONS = {
    # How long a task can stay "reserved" by a worker before Redis lets
    # another worker pick it up again if the first never acks (e.g. the
    # worker process was killed) — must exceed the longest task's time_limit.
    "visibility_timeout": int(os.getenv("CELERY_VISIBILITY_TIMEOUT", "3600")),
}
CELERY_TASK_TIME_LIMIT = int(os.getenv("CELERY_TASK_TIME_LIMIT", "600"))
CELERY_TASK_SOFT_TIME_LIMIT = int(os.getenv("CELERY_TASK_SOFT_TIME_LIMIT", "540"))
CELERY_WORKER_MAX_TASKS_PER_CHILD = int(os.getenv("CELERY_WORKER_MAX_TASKS_PER_CHILD", "200"))
CELERY_WORKER_PREFETCH_MULTIPLIER = int(os.getenv("CELERY_WORKER_PREFETCH_MULTIPLIER", "4"))
CELERY_TASK_ACKS_LATE = os.getenv("CELERY_TASK_ACKS_LATE", "True").lower() in ("1", "true", "yes")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "noreply@bazaar.local")
if EMAIL_HOST:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_USE_TLS = True

# Analysis defaults
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "1825"))  # ~5 calendar years (source may cap earlier)
MIN_HISTORY_DAYS = 60
CACHE_DIR = BASE_DIR / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Automatic live market sync (ticker) — no Redis required
AUTO_MARKET_SYNC = os.getenv("AUTO_MARKET_SYNC", "True").lower() in ("1", "true", "yes")
AUTO_SYNC_INTERVAL_MARKET = int(os.getenv("AUTO_SYNC_INTERVAL_MARKET", "60"))  # seconds while market open
AUTO_SYNC_INTERVAL_OFF = int(os.getenv("AUTO_SYNC_INTERVAL_OFF", "900"))  # seconds after hours
AUTO_DAILY_APPEND = os.getenv("AUTO_DAILY_APPEND", "True").lower() in ("1", "true", "yes")
AUTO_ANALYZE_AFTER_APPEND = os.getenv("AUTO_ANALYZE_AFTER_APPEND", "True").lower() in ("1", "true", "yes")
AUTO_CLOSE_LEARN = os.getenv("AUTO_CLOSE_LEARN", "True").lower() in ("1", "true", "yes")
DSE_SSL_VERIFY = os.getenv("DSE_SSL_VERIFY", "True").lower() not in ("0", "false", "no")

# --- Structured logging (Phase 9) --------------------------------------
# Every environment gets the same correlation-id + redaction pipeline
# (see config/logging_utils.py) — redaction is not something to turn off
# in dev, since local terminal output/log files get pasted into bug
# reports too. Only the formatter class differs: human-readable here,
# JSON in production.py (log aggregators want structured fields).
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "correlation": {"()": "config.logging_utils.CorrelationFilter"},
    },
    "formatters": {
        "console": {
            "()": "config.logging_utils.RedactingFormatter",
            "format": "%(asctime)s %(levelname)s [req=%(request_id)s task=%(task_id)s] %(name)s: %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "filters": ["correlation"],
            "formatter": "console",
        },
    },
    "root": {"handlers": ["console"], "level": os.getenv("DJANGO_LOG_LEVEL", "INFO")},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}

# Help requests/bdshare find CA certs on macOS Python installs
try:
    import certifi

    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except Exception:
    pass

# Some real-world CA chains (e.g. dsebd.org's, seen in practice) verify
# fine against the OS-native trust store but fail against certifi's
# static bundle alone — the server doesn't send its intermediate cert,
# and only OS-level trust stores resolve it (via cached/AIA-fetched
# intermediates; certifi + a bare ssl context do not do this). Injecting
# truststore makes every ssl.SSLContext in this process — including
# ones the third-party `bdshare` package builds internally, and our own
# dse_fetcher/cse_fetcher sessions — verify against the OS trust store
# instead, the same one curl/the browser use. Must run before any
# requests/urllib3 session is constructed, so it lives here, at settings
# import time (about as early as it gets). No-ops safely if the
# `truststore` package isn't installed.
#
# Only fixes this on macOS/Windows, though — their OS trust evaluation
# does AIA-chasing (fetches a missing intermediate on the fly); a bare
# Linux/OpenSSL trust store does not, so this same DSE/CSE cert-chain gap
# reproduces inside Linux containers regardless of this setting. The
# Docker image fixes it separately by vendoring the two specific missing
# intermediates (see Dockerfile / docker/certs/).
try:
    import truststore

    truststore.inject_into_ssl()
except Exception:
    pass
