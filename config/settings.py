"""
Bazaar — Bangladesh stock market analysis platform (DSE + CSE).
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# Project local time = Bangladesh market time (override with TIME_ZONE in .env)
TIME_ZONE = os.getenv("TIME_ZONE", "Asia/Dhaka").strip() or "Asia/Dhaka"
os.environ["TZ"] = TIME_ZONE
try:
    import time as _time

    if hasattr(_time, "tzset"):
        _time.tzset()
except Exception:
    pass

SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-bazaar-change-me")
DEBUG = os.getenv("DEBUG", "True").lower() in ("1", "true", "yes")
ALLOWED_HOSTS = [h.strip() for h in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]

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
}

CORS_ALLOW_ALL_ORIGINS = DEBUG

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_ALWAYS_EAGER = os.getenv("CELERY_TASK_ALWAYS_EAGER", "False").lower() in ("1", "true", "yes")

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

# Help requests/bdshare find CA certs on macOS Python installs
try:
    import certifi

    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except Exception:
    pass
