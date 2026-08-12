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

Everything else has a documented default (see `.env.production.example`),
including the exchange feature flags below.

## Exchange feature flags (DSE-only deployment)

`ENABLE_DSE` (default `True`) / `ENABLE_CSE` (default `False`) control
which exchanges this deployment actively fetches, analyzes, trains
models for, and exposes to public discovery — a fresh production
deployment with no override is **DSE-only by default**. `MAINTENANCE_MODE`
is a documented escape hatch for a deliberate full-outage window (both
flags false is otherwise a startup-time `ImproperlyConfigured` error).

This is an **operational toggle, not a data migration** — no schema
change, no destructive migration, nothing to run before or after
flipping it. Restart the web process(es) and every Celery
worker/beat after changing it (settings are read once at process start).
Full behavior, the exact re-enablement/catch-up procedure, and the
per-layer enforcement details (public routes 404, background tasks skip
without a false failure, ML training/serving stays DSE-scoped, existing
portfolio/watchlist records stay readable) are documented in the
top-level [README.md's "Exchange feature flags" section](../README.md#exchange-feature-flags).

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

## PostgreSQL — verified live (2026-08-01)

Everything above was previously only structurally reviewed — `production.py`
was never actually run against a real Postgres instance (Phase 10 flagged
this explicitly). Closed that gap for real, against a disposable local
PostgreSQL 16 instance:

- `manage.py check --deploy` under `config.settings.production` with real
  `POSTGRES_*` env vars: clean, `System check identified no issues`.
- `manage.py migrate`: all 47 migrations across every app applied cleanly
  to a fresh Postgres database — no SQLite-only assumption broke.
- Full test suite run against real Postgres (not SQLite):
  **418/419 pass.** The one failure
  (`test_backup_restore.BackupBazaarCommandTests.test_writes_db_copy_checksums_and_manifest`)
  is expected — it asserts a `source.sqlite3` file exists, which is
  specific to `backup_bazaar`'s SQLite branch and doesn't apply when the
  configured engine is Postgres; not a bug.
- One test-environment-only finding: `market/services/daily_append.py`
  calls `close_old_connections()` (correct and necessary in a real
  long-running Celery worker against Postgres, to avoid stale-connection
  errors) — but under Django's `TestCase` transactional test isolation,
  this closes the connection the test's own wrapping transaction depends
  on, surfacing as `OperationalError: the connection is closed` in
  `test_task_idempotency.AppendDailyBarsIdempotencyTests.test_running_append_twice_does_not_duplicate_todays_bar`
  specifically under Postgres (SQLite's connection handling doesn't
  trigger it). This is a test-harness artifact, not a production bug — no
  code fix applied; flagging here since it means that one specific test
  doesn't currently prove its claim when run under Postgres.
- **Real backup/restore drill, live** (closes the exact gap Phase 10
  flagged as unverified — "no live Postgres instance exists here to prove
  a real row-count-verified restore"): seeded representative data (20
  `Stock`, 600 `PriceHistory`, 20 `AnalysisResult`, 21 `MarketHoliday`
  rows), ran `manage.py backup_bazaar` (real `pg_dump`, custom format),
  restored it with `pg_restore` into a separate disposable database, and
  confirmed **exact row-count matches across every model** plus an
  exact-value spot check on a real row. `manage.py verify_backup` also
  passes its structural check now that `pg_restore` is actually on PATH.
  The disposable restore-target database and seed data were dropped
  afterward — this environment's real `db.sqlite3` was never touched.

## Redis / Celery

`CELERY_BROKER_URL` must use `rediss://` (TLS) with authentication in
production; plaintext `redis://` is refused at settings-import time
unless `CELERY_BROKER_ALLOW_PLAINTEXT=True` is set (only appropriate if
the broker is reachable exclusively over a private network you control
— e.g. same-VPC-only security groups).

The self-hosted Compose Redis service enables AOF with `appendfsync
everysec` and stores `/data` in the `redis_data` named volume. This makes
broker restarts substantially less likely to lose queued tasks, but it is
not a zero-loss guarantee. Task idempotency remains required.

Worker/timeout settings (overridable via env, see
`.env.production.example`): `CELERY_TASK_TIME_LIMIT` (hard kill),
`CELERY_TASK_SOFT_TIME_LIMIT` (raises inside the task first),
`CELERY_WORKER_MAX_TASKS_PER_CHILD` (recycle worker processes to bound
memory growth), `CELERY_WORKER_CONCURRENCY`/`CELERY_WORKER_PREFETCH_MULTIPLIER`
(conservative defaults for the documented VPS target, see below),
`CELERY_BROKER_CONNECTION_TIMEOUT`, and a `visibility_timeout` transport
option so a crashed worker's in-flight task is eventually retried by
another worker rather than lost forever.

Run the worker and beat scheduler as separate processes/services (see
README "Hybrid automation"), each with
`DJANGO_SETTINGS_MODULE=config.settings.production` exported. **Between
however many worker processes you run, every one of the four queues
(`market-fast,market-analysis,market-heavy,notifications`, plus Celery's
own `celery` queue) must be covered by at least one worker's `-Q` flag** —
Celery only consumes the queue(s) it's told to, and skipping one means
whatever routes there (live quote sync, daily append, full analysis, ML
training, digests, feedback notifications) silently never runs.
`docker-compose.yml` covers all four across two worker processes — see
"Self-hosted on a laptop via Docker" below.

### VPS sizing

Documented target: **Oracle Cloud "Always Free" A1 Flex — 2 OCPUs, ~12GB
RAM, ~20 concurrent users.** `docker-compose.yml` splits the worker into
two processes for real resource isolation — `celery-worker-light`
(`market-fast,market-analysis,notifications,celery`, concurrency 2) and
`celery-worker-heavy` (`market-heavy` only, concurrency 1) — so a
live-quote sync or digest never queues behind a full-analysis/ML-training
run, even though both are already scheduled for daily off-hours windows.
Tune `CELERY_WORKER_CONCURRENCY_LIGHT`/`_HEAVY` (see README's "Worker
recommendations") if profiling shows either process genuinely can't keep
up.

### Inspecting task status / recovering a stuck task

- **Admin Panel → Automation** — last success/failure per stage, currently-
  running tasks, recent failures with a one-click **Retry**.
- **Django Admin → Market → Task runs** — the full `TaskRun` history,
  filterable by status/task name.
- A run stuck in `started` for longer than ~20 minutes almost always means
  its worker process died mid-task (see the `stuck_job_*` alert on the Ops
  Report page) — the Redis-backed lock it was holding
  (`market.services.locking.distributed_lock`) auto-expires on its own
  `timeout`, so a new run isn't permanently blocked; use **Retry** on the
  Admin Panel once you've confirmed the old worker really is gone (check
  `docker compose ps celery-worker-light celery-worker-heavy` /
  `docker compose logs celery-worker-light` or `celery-worker-heavy`).
- `docker compose restart celery-worker-light` (or `celery-worker-heavy`)
  recovers a wedged worker without touching the other worker, `celery-beat`,
  or losing the schedule.

### Telegram ML daily report — setup & troubleshooting

See README's "Telegram ML daily report" section for the full behavior
(schedule, precision definitions, evidence levels, duplicate prevention).
Deployment-specific notes:

- Create the bot once via [@BotFather](https://t.me/BotFather); the token
  goes in `TELEGRAM_BOT_TOKEN` (secrets store / `.env.docker`, never git).
- Get `TELEGRAM_ADMIN_CHAT_ID` by messaging the bot once, then reading
  `message.chat.id` from `https://api.telegram.org/bot<token>/getUpdates` —
  do this from a terminal/browser you control, never accept a chat id from
  an inbound web request.
- Nothing sends until both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ADMIN_CHAT_ID`
  are set — until then the report still generates daily and is previewable
  from the Admin Panel, so this is safe to deploy before the bot exists.
- If the report stops sending: check Admin Panel → Telegram ML Report for
  the last failure (redacted — never shows the token or full Telegram
  response), then Django Admin → Notifications → Ml daily report deliveries
  for the full history. Three consecutive failed report dates also raise an
  Operational Alert.
- A stuck/misconfigured chat id (bot blocked, wrong id) fails permanently
  (not retried indefinitely) — fix the id/unblock the bot, then use
  **Send / retry today's report** on the Admin Panel.

## Static & media files

Static files are served by **WhiteNoise** directly from the Django
process (`whitenoise.middleware.WhiteNoiseMiddleware`,
`CompressedManifestStaticFilesStorage`) — no separate static file server
or object storage service is introduced. Media (`MEDIA_ROOT`) stays on
local disk; the app currently has no user-uploaded file fields, so this
is unused today but is configured consistently with `STATIC_ROOT`.

A repository-wide model and request-handler inspection found no
`FileField`, `ImageField`, or upload handler. The Compose stack therefore
does not add an otherwise-unused media volume. Add one before introducing
the first upload-backed model or endpoint.

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

There is no public sign-up (see the README's "Roles & permissions"
section) — after the first `migrate`, create the first Admin account
before anyone can log in at all:

```bash
python manage.py createsuperuser
```

Every subsequent account (Staff or User) is created from that Admin's
**Accounts → Create User / Create Staff** page, not from the shell.

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

## Render (recommended host, not Vercel)

Vercel was evaluated and ruled out: it's a serverless-functions-only
platform (no persistent processes), and this app needs three long-lived
services — gunicorn (web), a Celery worker, and Celery beat (the 60s live
market sync, scheduled daily append, ML training, close-learn settlement,
and monthly holiday sync all depend on beat actually staying up). Vercel
Cron is HTTP-triggered on a coarse schedule (daily on Hobby, 1-minute
minimum on Pro) and would need every task rewritten as a standalone
endpoint — a different, more limited architecture, not a deployment of
this one.

`render.yaml` at the repo root is a Render Blueprint that maps this app's
existing shape directly onto Render's service types:

| This app | Render resource |
|---|---|
| gunicorn web process | `bazaar-web` (`type: web`) |
| `celery -A config worker` | `bazaar-celery-worker` (`type: worker`, with a persistent disk at `data/` for ML model `.pkl` files — see the file's comments for why only this one service needs it) |
| `celery -A config beat` | `bazaar-celery-beat` (`type: worker`, no disk — beat only enqueues, never touches `data/cache/`) |
| PostgreSQL | `bazaar-postgres` (managed database) |
| Redis (Celery broker) | `bazaar-redis` (managed Key Value, private-network-only) |

**Deploy steps:**

1. Push `render.yaml` (already in this repo) to GitHub.
2. In the Render dashboard: **New +** → **Blueprint** → connect the
   `bazaar-market-analyzer` repo. Render reads `render.yaml` automatically.
3. Generate one secret and reuse it for **all three** `SECRET_KEY`
   prompts (`sync: false` on purpose — Render blueprints can't reliably
   share one generated value across services, so this must be done by
   hand; a mismatched key across processes breaks session/cookie
   validity):
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(64))"
   ```
4. Leave `ALLOWED_HOSTS`/`CSRF_TRUSTED_ORIGINS` on `bazaar-web` as
   anything for the first deploy — they can't be correct yet since the
   hostname doesn't exist until after deploy.
5. After the first deploy, note the assigned `*.onrender.com` URL (or
   your custom domain), then in the `bazaar-web` service's environment
   settings set:
   - `ALLOWED_HOSTS=<your-hostname>`
   - `CSRF_TRUSTED_ORIGINS=https://<your-hostname>`

   and redeploy `bazaar-web`.
6. Verify: `curl https://<your-hostname>/health/live` should return 200;
   check the `bazaar-celery-worker` and `bazaar-celery-beat` service logs
   for a clean startup (no `ImproperlyConfigured` errors — those mean an
   env var is missing/wrong, not a Postgres/Redis problem).

**Cost note:** background workers/beat have no free tier on Render, so
this is at minimum three paid `starter`-plan services plus a Key Value
instance; `bazaar-postgres` defaults to Render's free Postgres tier in
`render.yaml`; it has retention limits — switch its `plan` before
trusting it with real data. Check Render's current pricing before
deploying; this file doesn't guess at exact dollar amounts.

## Self-hosted on a laptop via Docker (sma.is)

An alternative to Render: run the whole stack in Docker Compose on a
personal machine, exposed publicly via a Cloudflare Tunnel (no router
port-forwarding, works behind a dynamic home IP, TLS handled by
Cloudflare's edge). Set up 2026-08-01 for domain `sma.is`.

**Files:**

| File | Purpose |
|---|---|
| `Dockerfile` | One image shared by web/worker/beat — `SERVICE_ROLE` env var picks the role. |
| `docker-entrypoint.sh` | Waits for Postgres, then (web only) runs `migrate` + `collectstatic` before exec'ing the real command — worker/beat skip this to avoid three containers racing to migrate at once. |
| `docker-compose.yml` | `db` (Postgres 16), `redis` (7), `web`, `celery-worker-light`, `celery-worker-heavy`, `celery-beat`, `cloudflared`. |
| `.env.docker.example` | Template — copy to `.env.docker` (gitignored) and fill in. **Not** the same file as the repo's plain `.env`, which is for native (non-Docker) local dev and must not be touched by this setup. |
| `scripts/update.sh` | `git pull` + rebuild + redeploy — the manual update path. |
| `scripts/docker-autostart.sh` + `scripts/com.bazaar.docker-autostart.plist` | macOS `launchd` LaunchAgent — starts Docker Desktop and the compose stack automatically at login. |

**Data persistence:** `./data` is bind-mounted into
`web`/`celery-worker-light`/`celery-worker-heavy` at `/app/data`, so ML
model artifacts (`data/cache/*.pkl`, only ever read/written by a worker —
web only reads precomputed DB rows) and
`data/backups/` survive container rebuilds. `backup_bazaar` defaults to
that persistent path (`/app/data/backups`) and the image includes matching
PostgreSQL 16 `pg_dump`/`pg_restore` tools. Postgres data lives in the named volume
`postgres_data`, Redis AOF data in `redis_data`, and `celery-beat`'s own
schedule-state file lives in the named volume `beat_data`
(`/app/beat/celerybeat-schedule`) — all are independent of the containers
themselves. Exactly one
`celery-beat` container is ever defined in `docker-compose.yml`; don't scale
that service beyond `1` replica, or the schedule fires more than once per
tick.

**One-time setup:**

1. `cp .env.docker.example .env.docker` and fill in `SECRET_KEY`
   (`python -c "import secrets; print(secrets.token_urlsafe(64))"`) and
   `POSTGRES_PASSWORD` (any strong random string).
2. **Cloudflare Tunnel** (needs your own Cloudflare account — sma.is's
   nameservers must point at Cloudflare first):
   - Cloudflare dashboard → **Zero Trust** → **Networks** → **Tunnels** →
     **Create a tunnel** → choose **Docker** as the connector.
   - Name it (e.g. `bazaar`), copy the token out of the sample
     `docker run ... --token <TOKEN>` command it shows you.
   - Paste that token into `.env.docker`'s `CLOUDFLARE_TUNNEL_TOKEN`.
   - In the same tunnel's **Public Hostname** tab, add: hostname `sma.is`
     (or `www.sma.is`), service type `HTTP`, URL `web:8000` (the Docker
     Compose service name/port — cloudflared reaches it over the compose
     network, not localhost).
   - Compose runs this named tunnel by default and refuses to interpolate
     without `CLOUDFLARE_TUNNEL_TOKEN`. Quick Tunnels are development-only;
     if needed, run cloudflared separately rather than weakening the
     production Compose service.
3. `docker compose --env-file .env.docker up -d --build` — first build
   compiles some Python dependencies from source (this project targets a
   very new Python version; not every package has a prebuilt wheel for it
   yet), so it can take a while the first time.
4. Verify: `curl -I https://sma.is/health/live/` should return `200`.
   For Oracle Ampere A1 and encrypted off-site backup operations, also see
   [`ORACLE_DEPLOYMENT.md`](ORACLE_DEPLOYMENT.md).
5. **Auto-start on boot/login:**
   ```bash
   mkdir -p ~/Library/Logs/bazaar-docker
   cp scripts/com.bazaar.docker-autostart.plist ~/Library/LaunchAgents/
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.bazaar.docker-autostart.plist
   ```
   This launches Docker Desktop and brings the compose stack up whenever
   you log in — no need to run anything by hand after a reboot. Logs at
   `~/Library/Logs/bazaar-docker/`.

**Updating code** (deliberately manual, not automatic — the app's own
data pipeline, live sync/daily append/ML training/holiday refresh, keeps
running automatically via Celery beat regardless; only *code* changes
require this step):

```bash
bash scripts/update.sh
```

Pulls `main`, rebuilds the image, and recreates the containers (migration
and static collection happen automatically via `docker-entrypoint.sh` on
`web`'s next startup).

**Known tradeoffs of this approach vs. Render** (see the laptop-hosting
discussion in chat history): the site's uptime is now this laptop's
uptime — sleep, reboots, power loss, and home network outages all take
it down; there's no managed backup/redundancy. `docker-autostart.sh`
handles the login/reboot case, but the machine still needs to actually be
powered on and connected to the internet for the site to be reachable.

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
