# Deployment (Phase 8)

This document covers running Bazaar in production. It does **not** stand up
any external service itself — provisioning a server, database, Redis
instance, TLS certificate, etc. is an infrastructure decision for whoever
operates this deployment, made outside of this repo.

## Settings modules

Settings are split into `config/settings/`:

| Module | Used by default? | Purpose |
|---|---|---|
| `base.py` | Never directly | Shared config. Not safe to run as-is (`SECRET_KEY`/`ALLOWED_HOSTS` are inert placeholders). |
| `development.py` | Yes — `manage.py`, `wsgi.py`, `asgi.py`, `celery.py` all default here | Local `runserver` work. `DEBUG=True` by default, insecure cookies, open CORS. |
| `test.py` | No (opt-in) | `DJANGO_SETTINGS_MODULE=config.settings.test python manage.py test` — eager Celery, fast password hasher, locmem email. The existing test suite is validated against **both** `development.py` (the default `manage.py test` uses) and `test.py`; switching the default was avoided so no already-passing test's behavior changes silently. |
| `production.py` | No — must be set explicitly | Fails fast (raises `ImproperlyConfigured`) if required env vars are missing, a placeholder, or otherwise unsafe. |

To run under production settings, the deploy environment (systemd unit,
Dockerfile `ENV`, Procfile, etc.) **must** export:

```bash
export DJANGO_SETTINGS_MODULE=config.settings.production
```

before `manage.py`, `gunicorn`, or `celery` starts. If this is forgotten,
the process falls back to `development.py` (safe defaults for a
developer's laptop, but wrong for a public deployment) rather than
crashing — there is no way to distinguish "operator forgot to set the
env var" from "this genuinely is a dev machine" without adding an
extra signal. Treat "is `DJANGO_SETTINGS_MODULE` set correctly" as a
required item on the deploy checklist / smoke test, not something the
code can fully self-detect.

## Required environment variables (production)

See `.env.production.example` for the full annotated template. Required
(the process refuses to start without these):

- `SECRET_KEY` — random, >=32 chars, not a known placeholder.
- `ALLOWED_HOSTS` — comma-separated hostnames.
- `CSRF_TRUSTED_ORIGINS` — comma-separated origins **with scheme**.
- `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`.
- `CELERY_BROKER_URL` — must be `rediss://` (TLS) unless
  `CELERY_BROKER_ALLOW_PLAINTEXT=True` is set explicitly.

Everything else has a documented default (see `.env.production.example`).

## Security headers / HTTPS

`config/settings/production.py` turns on, unconditionally:

- `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE` — cookies never sent over
  plain HTTP.
- `SECURE_HSTS_SECONDS=31536000` (1y) + subdomains + preload.
- `SECURE_CONTENT_TYPE_NOSNIFF`, `SECURE_REFERRER_POLICY=same-origin`,
  `X_FRAME_OPTIONS=DENY`.
- `SECURE_SSL_REDIRECT` (default `True`, override via env) +
  `SECURE_PROXY_SSL_HEADER` — **assumes a reverse proxy terminates TLS**
  and forwards `X-Forwarded-Proto`. If Django/gunicorn terminates TLS
  directly instead of sitting behind such a proxy, remove
  `SECURE_PROXY_SSL_HEADER` first — trusting that header from a client
  that talks to Django directly lets the client spoof `https`.

`development.py` and `test.py` explicitly turn the cookie/HSTS/redirect
settings back off — both run over plain `http://localhost`, where secure
cookies would silently break login. That's the one documented,
intentional visual/behavioral difference from production and it's
scoped to non-production settings modules only.

## Database: SQLite → PostgreSQL

Production uses PostgreSQL (`config/settings/production.py`); development
and tests keep using SQLite (`db.sqlite3`) — no behavior change there.

**This migration is never automatic.** No code in this repo runs it for
you. The procedure, run by a human when actually cutting over:

1. **Stop writers.** Pause Celery beat/workers and the web process (or at
   least put the app in maintenance mode) so SQLite stops changing
   mid-export.
2. **Back up SQLite** (same pattern as every prior phase):
   ```bash
   cp db.sqlite3 data/backups/db.sqlite3.pre-postgres-migration-$(date +%Y%m%d-%H%M%S).bak
   ```
3. **Export data** with Django's own serializer (portable across
   backends, unlike a raw SQLite dump):
   ```bash
   DJANGO_SETTINGS_MODULE=config.settings.development \
     python manage.py dumpdata --natural-foreign --natural-primary \
     --exclude admin.logentry --exclude contenttypes --exclude auth.permission \
     --output data/backups/full_dump_$(date +%Y%m%d-%H%M%S).json
   ```
4. **Provision PostgreSQL** and set `POSTGRES_*` env vars (see
   `.env.production.example`).
5. **Create the schema** on the empty Postgres database:
   ```bash
   DJANGO_SETTINGS_MODULE=config.settings.production python manage.py migrate
   ```
6. **Load the data:**
   ```bash
   DJANGO_SETTINGS_MODULE=config.settings.production \
     python manage.py loaddata data/backups/full_dump_<timestamp>.json
   ```
7. **Verify before cutting traffic over** — compare row counts per model
   between SQLite and Postgres (e.g. `Stock.objects.count()`,
   `PriceHistory.objects.count()` should match exactly; see Phase 6's
   provenance report for the pre-migration baseline counts), spot-check
   a few `AnalysisResult`/`PriceHistory` rows, and run the full test
   suite against the new database.
8. **Cut over**: point the running app at `DJANGO_SETTINGS_MODULE=config.settings.production`
   with the Postgres env vars, restart web + worker + beat.
9. **Keep the SQLite backup and the JSON dump** until the new database has
   been running successfully for a reasonable burn-in period — don't
   delete the only pre-migration copy on day one.

### Rollback

If Postgres cutover fails or surfaces a data problem:

1. Stop the web/worker/beat processes pointed at Postgres.
2. Restore `db.sqlite3` from the timestamped backup in step 2 (only
   needed if something modified it after the backup, e.g. a
   partially-applied migration attempt against SQLite — normally the
   backup is just insurance and the original file is untouched).
3. Restart the app with `DJANGO_SETTINGS_MODULE=config.settings.development`
   (or a from-source `production.py` copy still pointed at SQLite — not
   provided out of the box since production.py hardcodes the Postgres
   engine) until the Postgres issue is diagnosed.
4. The Postgres database itself is left in place for inspection — it's
   cheaper to diagnose-and-retry a cutover than to have deleted the
   evidence.

## Redis / Celery

`CELERY_BROKER_URL` must use `rediss://` (TLS) with authentication in
production; plaintext `redis://` is refused at settings-import time
unless `CELERY_BROKER_ALLOW_PLAINTEXT=True` is set (only appropriate if
the broker is reachable exclusively over a private network you control
— e.g. same-VPC-only security groups).

Worker/timeout settings (overridable via env, see
`.env.production.example`): `CELERY_TASK_TIME_LIMIT` (hard kill),
`CELERY_TASK_SOFT_TIME_LIMIT` (raises inside the task first),
`CELERY_WORKER_MAX_TASKS_PER_CHILD` (recycle worker processes to bound
memory growth), `CELERY_WORKER_PREFETCH_MULTIPLIER`,
`CELERY_BROKER_CONNECTION_TIMEOUT`, and a `visibility_timeout` transport
option so a crashed worker's in-flight task is eventually retried by
another worker rather than lost forever.

Run the worker and beat scheduler as separate processes/services (see
README "Background execution"), each with
`DJANGO_SETTINGS_MODULE=config.settings.production` exported.

## Static & media files

Static files are served by **WhiteNoise** directly from the Django
process (`whitenoise.middleware.WhiteNoiseMiddleware`,
`CompressedManifestStaticFilesStorage`) — no separate static file server
or object storage service is introduced. Media (`MEDIA_ROOT`) stays on
local disk; the app currently has no user-uploaded file fields, so this
is unused today but is configured consistently with `STATIC_ROOT`.

Deploy step (run once per deploy, before starting gunicorn):

```bash
DJANGO_SETTINGS_MODULE=config.settings.production python manage.py collectstatic --noinput
```

This copies/hashes/compresses everything under `static/` into
`staticfiles/`, which WhiteNoise then serves with cache-forever headers
(safe because the filenames are content-hashed).

**Not decided / would need separate authorization:** moving static/media
to S3 or a CDN. WhiteNoise is suffient for a single-server deployment;
revisit only if traffic/scale actually requires it.

## Running the app in production

```bash
export DJANGO_SETTINGS_MODULE=config.settings.production
# ... export the rest of .env.production.example's variables ...

python manage.py migrate            # schema only — see the manual data-migration procedure above for a fresh Postgres cutover
python manage.py collectstatic --noinput
python manage.py check --deploy     # see below

gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3
celery -A config worker --loglevel=info
celery -A config beat --loglevel=info
```

## Deployment checks

```bash
DJANGO_SETTINGS_MODULE=config.settings.production python manage.py check --deploy
```

With a fully-populated, valid `.env.production.example`-shaped
environment this currently reports:

```
System check identified no issues (0 silenced).
```

Run this as part of every deploy — a newly-introduced insecure setting
will fail it (or fail the fail-fast `ImproperlyConfigured` checks in
`production.py` first, before Django's own checks even run).

## Dependency pinning & vulnerability scanning

- `requirements.txt` — direct dependencies, exact-pinned.
- `requirements-lock.txt` — full transitive closure (`pip freeze`),
  regenerate per the header comment in that file. Production installs
  should use this file: `pip install -r requirements-lock.txt`.
- Scan with [`pip-audit`](https://pypi.org/project/pip-audit/):
  ```bash
  pip install pip-audit
  pip-audit
  ```
  Last run (2026-07-30): **0 known vulnerabilities** across the full
  installed environment. (An earlier run flagged 5 known CVEs in `pip`
  25.3 itself — fixed by upgrading pip to 26.1.2; not an application
  dependency, but worth keeping current since it's part of the build
  toolchain.)
- No currently-justified exceptions exist. If a future scan finds a
  vulnerability with no available fix, document the package, the CVE,
  why the vulnerable code path isn't reachable here, and a re-check date
  in this section rather than silently ignoring it.

## Remaining infrastructure decisions (not made here)

These need an explicit choice from whoever operates the real deployment
— none of them are wired up, by design, since they're infrastructure
changes outside this repo's authorization:

- **Where it runs**: bare VM, container platform, PaaS — not chosen.
- **TLS termination**: which reverse proxy/load balancer sits in front
  (nginx, Caddy, a cloud LB) and issues/renews the certificate.
- **Postgres/Redis hosting**: managed service vs. self-hosted; backup
  schedule and retention for the *new* Postgres database (separate from
  the SQLite backup procedure above).
- **Static/media at scale**: WhiteNoise is the default; S3/CDN is a
  later option if needed.
- **Log/metrics shipping**: `production.py` logs to stdout/stderr
  (12-factor); whether that's Docker's log driver, journald, or a
  shipped-to-a-vendor pipeline is unset.
- **Process supervision**: systemd units / Docker Compose / Kubernetes
  manifests for web + worker + beat are not included in this repo.
