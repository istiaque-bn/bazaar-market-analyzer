# Single image shared by all three long-lived processes (web, Celery
# worker, Celery beat) — docker-compose.yml picks the role per container
# via the SERVICE_ROLE env var and docker-entrypoint.sh's dispatch. Keeps
# one build, one set of installed dependencies, no drift between
# processes that must agree on model/task definitions.
FROM python:3.14-slim AS base

# postgresql-client-16: provides pg_dump/pg_restore matching the Compose
# PostgreSQL 16 server. Debian Trixie's default client is 17, whose dumps
# include settings PostgreSQL 16 does not recognize during restore, so use
# PostgreSQL's signed APT repository and pin the client major explicitly.
# PGDG publishes this package for both amd64 and arm64, including Oracle
# Ampere A1 hosts.
# libpq-dev/build-essential: psycopg[binary] ships its own libpq, but
# some transitive deps (lxml, scikit-learn/scipy/numpy) may fall back to
# source builds if no prebuilt wheel exists yet for this Python version —
# keep the toolchain available rather than fail obscurely mid-build.
# libgomp1: xgboost's OpenMP-parallelized training needs it at runtime;
# Debian slim doesn't ship it by default.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl --fail --show-error --silent \
        -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && . /etc/os-release \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
    build-essential \
    postgresql-client-16 \
    libpq-dev \
    libxml2-dev \
    libxslt1-dev \
    libgomp1 \
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
    && mkdir -p /app/data/cache /app/data/backups /app/staticfiles /app/beat \
    && chown -R bazaar:bazaar /app

USER bazaar

ENV DJANGO_SETTINGS_MODULE=config.settings.production \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "9"]
