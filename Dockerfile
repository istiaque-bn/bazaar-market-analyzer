# Single image shared by all three long-lived processes (web, Celery
# worker, Celery beat) — docker-compose.yml picks the role per container
# via the SERVICE_ROLE env var and docker-entrypoint.sh's dispatch. Keeps
# one build, one set of installed dependencies, no drift between
# processes that must agree on model/task definitions.
FROM python:3.14-slim AS base

# libpq-dev/build-essential: psycopg[binary] ships its own libpq, but
# some transitive deps (lxml, scikit-learn/scipy/numpy) may fall back to
# source builds if no prebuilt wheel exists yet for this Python version —
# keep the toolchain available rather than fail obscurely mid-build.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    libxml2-dev \
    libxslt1-dev \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# dsebd.org/dse.com.bd and cse.com.bd/www.cse.com.bd don't send their
# intermediate certificate during the TLS handshake — only clients whose
# OS trust store does AIA-chasing (macOS's Security.framework does; a
# bare Linux/OpenSSL trust store does not) recover from that
# automatically. Confirmed via `openssl s_client -showcerts` against both
# hosts (2026-08-02): verify error 21 "unable to verify the first
# certificate" even with a fully current `ca-certificates` package.
# Vendoring the two missing intermediates (fetched from each cert's own
# AIA "CA Issuers" URL, not a random source) and trusting them locally is
# the standard fix for exactly this — same effect as what a browser/curl
# already does silently, made explicit and reproducible at build time
# rather than depending on macOS-only truststore() behavior at runtime.
COPY docker/certs/*.crt /usr/local/share/ca-certificates/
RUN update-ca-certificates

WORKDIR /app

COPY requirements-lock.txt .
RUN pip install --no-cache-dir -r requirements-lock.txt

COPY . .

RUN useradd --create-home --uid 1000 bazaar \
    && mkdir -p /app/data/cache /app/data/backups /app/staticfiles \
    && chown -R bazaar:bazaar /app

USER bazaar

ENV DJANGO_SETTINGS_MODULE=config.settings.production \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
