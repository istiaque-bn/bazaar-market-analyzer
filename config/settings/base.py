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

from django.core.exceptions import ImproperlyConfigured
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
    "feedback.apps.FeedbackConfig",
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
    # After AuthenticationMiddleware (needs request.user): promptly logs
    # out a deactivated account's live session and forces a
    # temporary-password holder through the password-change screen.
    "accounts.middleware.AccountStateMiddleware",
    # After auth is resolved so it can see request.user; before
    # MessageMiddleware doesn't matter (it only touches headers, not body).
    "accounts.middleware.NoStoreForAuthenticatedMiddleware",
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
                "accounts.context_processors.role_context",
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
# Fallback only — accounts.views.BazaarLoginView.get_default_redirect_url()
# routes each role to its own panel (Admin/Staff/User) instead of always
# using this fixed value; kept for anything that calls the plain
# `django.contrib.auth.views.LoginView`/reverse("login") machinery
# directly (e.g. the admin's "you're not staff" bounce).
LOGIN_REDIRECT_URL = "login"
LOGOUT_REDIRECT_URL = "login"

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
# Conservative defaults for a small VM (documented target: Oracle A1, 2
# OCPUs, ~20 users) — a worker that only prefetches one task at a time
# can't hoard several quote-fetch jobs behind one slow ML-training run,
# and a single OS-level worker process keeps memory bounded. Both are
# still overridable via env for a beefier deployment.
CELERY_WORKER_PREFETCH_MULTIPLIER = int(os.getenv("CELERY_WORKER_PREFETCH_MULTIPLIER", "1"))
CELERY_WORKER_CONCURRENCY = int(os.getenv("CELERY_WORKER_CONCURRENCY", "1"))
CELERY_TASK_ACKS_LATE = os.getenv("CELERY_TASK_ACKS_LATE", "True").lower() in ("1", "true", "yes")

# --- Task queues ---------------------------------------------------------
# Named so a heavy job (ML training, full analysis) can be isolated to
# its own worker later without touching task code — a worker not yet
# split off just listens to all of them (see config/celery.py's
# task_routes and docker-compose.yml's celery-worker `-Q` flag).
CELERY_TASK_DEFAULT_QUEUE = "market-fast"
QUEUE_MARKET_FAST = "market-fast"  # quote sync, freshness checks — must never queue behind heavy work
QUEUE_MARKET_ANALYSIS = "market-analysis"  # intraday lightweight analysis, daily append
QUEUE_MARKET_HEAVY = "market-heavy"  # full analysis, ML training, reliability assessment
QUEUE_NOTIFICATIONS = "notifications"  # digests, feedback notifications

# Explicit per-task routing. Anything not listed falls back to
# CELERY_TASK_DEFAULT_QUEUE (market-fast) — deliberately the *cheapest*
# default so an unrouted new task doesn't accidentally land in front of
# quote fetching; every task with real cost is named here instead.
CELERY_TASK_ROUTES = {
    "market.tasks.sync_live_market": {"queue": QUEUE_MARKET_FAST},
    "market.tasks.sync_pe_ratios": {"queue": QUEUE_MARKET_FAST},
    "market.tasks.run_intraday_analysis": {"queue": QUEUE_MARKET_ANALYSIS},
    "market.tasks.append_daily_bars": {"queue": QUEUE_MARKET_ANALYSIS},
    "market.tasks.sync_holiday_calendar": {"queue": QUEUE_MARKET_ANALYSIS},
    "market.tasks.fetch_all_market_data": {"queue": QUEUE_MARKET_HEAVY},
    "market.tasks.run_full_analysis": {"queue": QUEUE_MARKET_HEAVY},
    "market.tasks.run_end_of_day_pipeline": {"queue": QUEUE_MARKET_HEAVY},
    "market.tasks.seed_demo_and_analyze": {"queue": QUEUE_MARKET_HEAVY},
    "market.tasks.train_ml_model": {"queue": QUEUE_MARKET_HEAVY},
    "market.tasks.close_learn_settlement": {"queue": QUEUE_MARKET_HEAVY},
    "market.tasks.assess_ml_reliability": {"queue": QUEUE_MARKET_HEAVY},
    "market.tasks.analyze_and_notify": {"queue": QUEUE_MARKET_HEAVY},
    "notifications.tasks.send_daily_digest": {"queue": QUEUE_NOTIFICATIONS},
    "notifications.tasks.send_ml_daily_report": {"queue": QUEUE_NOTIFICATIONS},
    "feedback.tasks.notify_reporter": {"queue": QUEUE_NOTIFICATIONS},
}

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# Separate bot, deliberately never shared with TELEGRAM_BOT_TOKEN above —
# used only for account-credential delivery (accounts.services), so a
# leaked/compromised general-notification bot token can't be used to
# phish temp passwords, and vice versa. Falls back to "not configured"
# (temp password shown on-screen only) exactly like the main bot when
# unset — see notifications.services.send_telegram_message's `token=`.
TELEGRAM_SECURITY_BOT_TOKEN = os.getenv("TELEGRAM_SECURITY_BOT_TOKEN", "")

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

# --- Hybrid automation (Celery Beat + feature-flagged scheduled tasks) --
# Every AUTO_* toggle below can be disabled independently; every interval
# is in seconds unless its name says otherwise, and is validated here
# (not at task-run time) so a typo'd .env fails at process startup, the
# same "fail fast, fail loud" rule production.py already applies to
# required secrets — see _positive_int().


def _bool_env(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes")


def _positive_int(name: str, default: str) -> int:
    raw = os.getenv(name, default)
    try:
        value = int(raw)
    except ValueError:
        raise ImproperlyConfigured(f"{name}={raw!r} is not a valid integer.")
    if value <= 0:
        raise ImproperlyConfigured(f"{name}={raw!r} must be a positive number of seconds (got {value}).")
    return value


# --- Telegram ML daily report --------------------------------------------
# One consolidated, plain-language ML status report sent to a single
# configured Admin recipient once a day. Independently configurable from
# TELEGRAM_CHAT_ID/the daily digest above — disabling one never disables
# the other. TELEGRAM_ADMIN_CHAT_ID is deliberately its own setting (never
# taken from a browser request) so only whoever controls deployment
# config decides who receives it — see market/services/ml_daily_report.py
# and notifications/tasks.py send_ml_daily_report.
TELEGRAM_ML_DAILY_REPORT = _bool_env("TELEGRAM_ML_DAILY_REPORT", "True")
TELEGRAM_ML_REPORT_TIME = os.getenv("TELEGRAM_ML_REPORT_TIME", "17:00").strip()
TELEGRAM_ML_REPORT_TIMEZONE = os.getenv("TELEGRAM_ML_REPORT_TIMEZONE", "Asia/Dhaka").strip()
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "").strip()
# Operational alerts (stale data, failed jobs, database/model health) use
# the same private admin chat as the ML report. A cooldown prevents a noisy
# outage from sending the same alert every scheduler tick.
TELEGRAM_OPS_ALERTS = _bool_env("TELEGRAM_OPS_ALERTS", "True")
TELEGRAM_OPS_ALERT_COOLDOWN_MINUTES = _positive_int("TELEGRAM_OPS_ALERT_COOLDOWN_MINUTES", "360")

try:
    _ml_report_hour, _ml_report_minute = (int(p) for p in TELEGRAM_ML_REPORT_TIME.split(":", 1))
    if not (0 <= _ml_report_hour <= 23 and 0 <= _ml_report_minute <= 59):
        raise ValueError
except ValueError:
    raise ImproperlyConfigured(f"TELEGRAM_ML_REPORT_TIME={TELEGRAM_ML_REPORT_TIME!r} must be 24-hour 'HH:MM'.")

try:
    from zoneinfo import ZoneInfo

    ZoneInfo(TELEGRAM_ML_REPORT_TIMEZONE)
except Exception as _tz_exc:
    raise ImproperlyConfigured(
        f"TELEGRAM_ML_REPORT_TIMEZONE={TELEGRAM_ML_REPORT_TIMEZONE!r} is not a valid IANA timezone name."
    ) from _tz_exc

# TELEGRAM_ML_DAILY_REPORT above just means "this feature is turned on" —
# it can still be effectively inert without credentials (same
# graceful-degradation pattern as TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID:
# never crash startup for missing secrets, since most dev/test
# environments won't have a real bot token). Whether a real send is
# actually attempted is computed at call time as
# `bool(settings.TELEGRAM_BOT_TOKEN) and bool(settings.TELEGRAM_ADMIN_CHAT_ID)`
# — deliberately NOT precomputed into a settings constant here, since
# that would freeze at settings-import time and silently stop tracking
# override_settings()/runtime credential changes.


# Automatic live market sync (ticker + portfolio quotes) — no Redis
# required to run the app, but Celery Beat drives the actual cadence.
AUTO_MARKET_SYNC = _bool_env("AUTO_MARKET_SYNC", "True")
AUTO_SYNC_INTERVAL_MARKET = _positive_int("AUTO_SYNC_INTERVAL_MARKET", "200")  # seconds, while DSE is open
# Outside-hours freshness check — set AUTO_SYNC_INTERVAL_OFF high (or
# AUTO_MARKET_SYNC=False) to disable the off-hours cadence entirely;
# there's no separate on/off switch for just this half since the
# interval-based check already self-throttles to "never" for any
# interval longer than the gap between trading sessions.
AUTO_SYNC_INTERVAL_OFF = _positive_int("AUTO_SYNC_INTERVAL_OFF", "3600")  # seconds, while DSE is closed

# Lightweight intraday analysis (technical snapshot refresh only — no
# predictor/pattern/ML recompute, no new AnalysisResult rows; see
# market/services/intraday_analysis.py) between full end-of-day passes.
AUTO_INTRADAY_ANALYSIS = _bool_env("AUTO_INTRADAY_ANALYSIS", "True")
AUTO_INTRADAY_ANALYSIS_INTERVAL = _positive_int("AUTO_INTRADAY_ANALYSIS_INTERVAL", "900")  # seconds

AUTO_DAILY_APPEND = _bool_env("AUTO_DAILY_APPEND", "True")
AUTO_ANALYZE_AFTER_APPEND = _bool_env("AUTO_ANALYZE_AFTER_APPEND", "True")
AUTO_CLOSE_LEARN = _bool_env("AUTO_CLOSE_LEARN", "True")
AUTO_PAPER_TRADING = _bool_env("AUTO_PAPER_TRADING", "True")
AUTO_PAPER_TRADING_INTERVAL = _positive_int("AUTO_PAPER_TRADING_INTERVAL", "900")  # seconds while its session window is open

# Daily ML training — retrains once a day at a fixed off-hours time
# (well before the 10:00 Asia/Dhaka market open, so it never collides
# with market hours or the daily end-of-day pipeline regardless of which
# calendar day it lands on). market.tasks.train_ml_model itself is a
# no-op when there's no new label-resolvable data since the last train,
# so a quiet day just logs "skipped: no new data" rather than doing
# wasted work.
AUTO_ML_TRAINING = _bool_env("AUTO_ML_TRAINING", "True")
AUTO_ML_TRAINING_TIME = os.getenv("AUTO_ML_TRAINING_TIME", "00:30").strip()

try:
    _ml_hour, _ml_minute = (int(p) for p in AUTO_ML_TRAINING_TIME.split(":", 1))
    if not (0 <= _ml_hour <= 23 and 0 <= _ml_minute <= 59):
        raise ValueError
except ValueError:
    raise ImproperlyConfigured(f"AUTO_ML_TRAINING_TIME={AUTO_ML_TRAINING_TIME!r} must be 24-hour 'HH:MM'.")

DSE_SSL_VERIFY = os.getenv("DSE_SSL_VERIFY", "True").lower() not in ("0", "false", "no")

# --- Exchange feature flags ---------------------------------------------
# Which exchanges this deployment actively fetches, analyzes, trains
# models for, and exposes to public discovery. Disabling an exchange is a
# purely operational toggle: it never deletes, mutates, or relabels any
# row, and is reversible by flipping the flag back and restarting
# services (see README.md's "Exchange feature flags" section). Every
# other module is expected to read market.services.exchange_config's
# enabled_exchanges()/is_exchange_enabled() rather than these two settings
# or os.environ directly, so this is the one place the raw env vars are
# parsed. development.py/test.py deliberately re-enable CSE on top of
# this default so the existing local-dev/test-suite behavior (which
# predates this flag and exercises CSE broadly) is unaffected unless a
# developer opts into DSE-only locally via their own .env.
ENABLE_DSE = os.getenv("ENABLE_DSE", "True").strip().lower() in ("1", "true", "yes")
ENABLE_CSE = os.getenv("ENABLE_CSE", "False").strip().lower() in ("1", "true", "yes")
# Escape hatch for a deliberate full-outage window (e.g. planned database
# maintenance) — lets ops take every exchange offline at once without the
# startup check below treating that as a misconfiguration.
MAINTENANCE_MODE = os.getenv("MAINTENANCE_MODE", "False").strip().lower() in ("1", "true", "yes")

if not ENABLE_DSE and not ENABLE_CSE and not MAINTENANCE_MODE:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured(
        "ENABLE_DSE and ENABLE_CSE are both false — this deployment would serve no market data at "
        "all. If this is intentional (e.g. planned maintenance), set MAINTENANCE_MODE=True to "
        "acknowledge it explicitly; otherwise enable at least one exchange."
    )

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
