# Bazaar — DSE & CSE Market Analyzer

Django platform that analyses **Dhaka (DSE)** and **Chittagong (CSE)** equities using ~1 year of history, then surfaces:

- Potential shares (ranked scores)
- Pattern detection (RSI, MACD, MA crosses, volume, Bollinger)
- Predictive estimates: *mature in ~X days*, *peak in ~Y days* (historical analogues — **not guarantees**)
- Safe-buy / sell suggestions
- Telegram / email / in-app digests
- Backtests + lightweight ML refinement
- REST API for mobile / integrations

## Quick start

```bash
cd /Users/istiaque/Desktop/Test/Trial
source .venv/bin/activate
cp .env.example .env
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser  # required — see "Roles & permissions" below; there is no public sign-up
python manage.py run_market_pipeline --all
python manage.py runserver
```

Open http://127.0.0.1:8000/ — you'll land on Login; there is no public
page or sign-up any more (see below).

Settings live in `config/settings/` (`base.py` + `development.py` /
`test.py` / `production.py`). `manage.py`/`wsgi.py`/`asgi.py`/`celery.py`
all default to `config.settings.development` — the commands above are
unchanged from before the split. For a production deployment (PostgreSQL,
security headers, WhiteNoise static files, etc.), see
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — that settings module is never
used unless `DJANGO_SETTINGS_MODULE=config.settings.production` is set
explicitly.

## Roles & permissions

Bazaar is an invite-only platform with three roles, sourced entirely from
Django's own `is_staff`/`is_superuser` flags — there is no separate `role`
field that could drift out of sync with them (`accounts/roles.py`):

| Role  | `is_superuser` | `is_staff` |
|-------|-----------------|------------|
| Admin | True            | True       |
| Staff | False           | True       |
| User  | False           | False      |

An inactive account (`is_active=False`) has no role for access purposes,
even though its `is_staff`/`is_superuser` flags are left untouched — see
"Deactivation" below.

### Public registration is removed

There is no self-service sign-up. `/accounts/signup/` is a tombstone —
GET and POST both return `403` and create nothing (`accounts.views.
signup_disabled`). The same is true of `/api/auth/register/`
(`api.views.RegisterAPI`). Accounts are created only by an Admin or Staff
member from **Accounts → Create User / Create Staff**, or the very first
Admin via `python manage.py createsuperuser`.

### Permission matrix

| Capability | Admin | Staff | User |
|---|---|---|---|
| Own portfolio, watchlist, alerts, profile | ✓ | ✓ | ✓ |
| Browse stocks, view analysis/predictions | ✓ | ✓ | ✓ |
| Create User accounts | ✓ | ✓ | ✗ |
| Create Staff accounts | ✓ | ✗ | ✗ |
| View/search all accounts | ✓ (all) | ✓ (Users only) | ✗ |
| Activate/deactivate a User | ✓ | ✓ | ✗ |
| Activate/deactivate a Staff/Admin | ✓ | ✗ | ✗ |
| Reset a User's password | ✓ | ✓ | ✗ |
| Reset a Staff/Admin's password | ✓ | ✗ | ✗ |
| Promote User → Staff / demote Staff → User | ✓ | ✗ | ✗ |
| Data QA / Operations reports | ✓ | ✓ | ✗ |
| ML Reliability Monitor | ✓ | ✗ | ✗ |
| Trigger fetch/analysis/training pipeline | ✓ | ✗ | ✗ |
| Django Admin (`/admin/`) | ✓ | ✗ | ✗ |

Every row is enforced **server-side** — by view decorators
(`accounts/decorators.py`: `admin_required`, `staff_or_admin_required`),
queryset scoping (Staff's account list/detail queries exclude Staff/Admin
rows entirely, so a direct URL to one 404s exactly like a nonexistent
account), and service-layer checks in `accounts/services.py` that a
decorator bypass alone can't get around. Manipulated POST fields
(`is_staff`, `is_superuser`, `role`, `groups`, `user_permissions`) are
never read — the account-creation forms don't declare those fields at
all, so there's nothing to mass-assign from.

**Response policy:** anonymous browser request → redirect to Login;
authenticated but wrong role → `403`; anonymous API request → `401`;
authenticated but wrong role via API → `403`.

### Protecting the last Admin

An Admin cannot deactivate the **last active Admin** account — neither
their own nor anyone else's — enforced in `accounts.services.set_active`
inside a DB transaction. There is no promotion/demotion path *to* or
*from* Admin through the UI; the only way to create one is
`createsuperuser` (see "Recovery" below).

### Account creation & temporary passwords

New accounts get a system-generated temporary password (never
admin-typed, never logged, never emailed in this deployment — the
console/SMTP email backend isn't treated as "reliably available" for a
password-reset flow). It's shown **once**, on the confirmation page,
immediately after creation or after **Reset password** on an existing
account. The new/reset account is flagged `must_change_password=True`
(`accounts.models.UserProfile`) and is redirected to a forced
password-change screen on next login before it can reach anything else
(`accounts.middleware.AccountStateMiddleware`).

Email uniqueness policy: a blank email is allowed on multiple accounts
(legacy/local accounts); a **non-blank** email must be unique across all
accounts.

### Deactivation & session security

Deactivating an account takes effect on its very next request — even
mid-session — via `AccountStateMiddleware`, which checks `is_active` on
every request (Django's own `AuthenticationMiddleware` only checks this
at login time, not on every subsequent request) and force-logs-out a
now-deactivated session.

### Login, redirects, and `next`

- Anonymous request to `/` → redirect to Login.
- Authenticated request to `/` or to Login → redirect to the caller's own
  role panel (`accounts.roles.role_home_url`): Admin → Admin Panel, Staff
  → Staff Panel, User → User Panel.
- `next` is validated by Django's own `url_has_allowed_host_and_scheme`
  (via `SuccessURLAllowedHostsMixin`) — an external or protocol-relative
  `next` is rejected and the caller falls back to their role panel.
- Login errors are generic ("Please enter a correct username and
  password") and don't distinguish a wrong password from an unknown
  username or an inactive account — `ModelBackend.user_can_authenticate`
  already refuses inactive users before Django's own extra "inactive"
  message would ever be reached.
- Logout is POST-only with CSRF protection (Django 5+'s `LogoutView`
  default).

### The three panels

- **Admin Panel** (`/accounts/panel/admin/`) — account counts by
  role/status, recent account creations and role/status changes, DSE
  (+CSE) market status, operational alerts, recent task runs, active ML
  models, and quick actions for every Admin capability above.
- **Staff Panel** (`/accounts/panel/staff/`) — User count, recently
  created Users, market status, operational alerts, recent task status,
  and links to Data QA / Operations / Create User / Manage Users.
- **User Panel** (`/accounts/panel/user/`) — portfolio summary (market
  value, unrealized P/L, today's P/L), watchlist count, recent
  transactions, personal alerts, and market status — with a plain
  "no transactions yet" empty state for a brand-new account.

### Navigation & the status strip

Each role sees only the nav links it's permitted to use (also enforced
server-side by the view behind every link). The date/time + DSE(/CSE)
open-closed cards used to live inside the header; they're now a
dedicated `.status-strip` block **below** the header/nav and **above**
page content, shown on every authenticated page and never on Login.
CSE's card is omitted entirely when `ENABLE_CSE` is false (see "Exchange
feature flags" above) — never shown as an empty placeholder.

### Testing

```bash
DJANGO_SETTINGS_MODULE=config.settings.test python manage.py test accounts market api notifications
```

Role/auth-specific tests live in `accounts/tests.py` (role helpers,
anonymous access, login redirects, deactivation-mid-session, Admin/Staff/
User account-management permissions, cross-user isolation, API privilege
escalation) and `market/tests/test_navbar.py` (nav visibility per role,
status-strip placement/structure, active-link state).

### Recovery if privileged access is lost

If every Admin account is somehow deactivated or its password lost,
recover from a shell with database access:

```bash
python manage.py createsuperuser        # mints a brand-new Admin, or
python manage.py shell -c "
from django.contrib.auth.models import User
u = User.objects.get(username='the-locked-out-admin')
u.is_active = True
u.is_staff = True
u.is_superuser = True
u.save()
"
```

Both paths bypass the web UI entirely (as intended — this is a
break-glass procedure, not a self-service one) and require direct access
to the running environment (`manage.py shell`/SSH/`docker compose exec`),
not just a browser session.

### Known limitations

- No account-management REST API — Admin/Staff use the web UI only; the
  existing DRF API surface (stocks/portfolio/alerts/ML-reliability) is
  unchanged apart from now requiring authentication throughout.
- No email-based password reset — a reliable outbound email service
  isn't configured in this environment, so temporary passwords + forced
  first-login change is the supported flow (see "Account creation" above).
  `password_reset_url` in the login template is wired for a future
  `django.contrib.auth` `PasswordResetView` if email becomes available.
- Admin↔Staff role changes aren't supported through the UI — only
  User↔Staff promotion/demotion. Minting/demoting an Admin is a
  deliberate `createsuperuser`/shell-only action (see "Recovery").
- `/health/`, `/health/live/`, `/health/ready/` remain unauthenticated —
  they're infrastructure liveness/readiness probes (load balancers can't
  authenticate) and expose no user data, so they're treated as outside
  the "anonymous may only reach Login" policy.

## Phases covered

| Phase | Status |
|-------|--------|
| 1. MVP: fetch/store ~1y + screener | Done |
| 2. Indicators, patterns, Telegram alerts | Done |
| 3. Backtests + maturity/peak estimates | Done |
| 4. CSE + accounts + dashboard | Done |
| 5. ML blend + REST API | Done |
| 6. Data provenance & quality | Done |
| 7. Honest forecast/signal presentation | Done |
| 8. Production hardening (settings split, security, Postgres/Celery config, static, deps) | Done |
| 9. Operational readiness (structured logs, health checks, ops metrics/alerts, backups, audit trail, runbooks) | Done |
| 10. Final independent release-readiness audit | Done — see [`docs/RELEASE_READINESS.md`](docs/RELEASE_READINESS.md) |

## Commands

```bash
# Seed synthetic 1y data for 15 symbols × DSE/CSE and analyze
python manage.py run_market_pipeline --demo --analyze

# Try live DSE/CSE fetch (falls back gracefully if offline)
python manage.py run_market_pipeline --fetch --analyze

# Full pipeline
python manage.py run_market_pipeline --all
```

## Hybrid automation (Celery + Celery Beat)

**Redis is required.** Every piece of background work — live quote sync,
lightweight intraday analysis, daily OHLCV append, full analysis, forecast
settlement, reliability assessment, ML training, digests, feedback
notifications — runs as a named, scheduled Celery task
(`market/tasks.py`, `notifications/tasks.py`, `feedback/tasks.py`). Nothing
starts a thread at process startup, and no web request ever performs a slow
DSE fetch, a full analysis pass, or ML training directly — every Admin
button and every scheduled job funnels through the exact same task
functions, so there is only one code path to reason about for each piece of
work, not one for "automatic" and a different one for "manual".

```text
Celery Beat
    -> Scheduled task tick (self-throttling; see "Beat schedule" below)
    -> Distributed lock (market.services.locking / autosync.exclusive_db_write)
    -> Market-hours + holiday + exchange-flag check
    -> Fetch and validate DSE data (market.services.dse_fetcher)
    -> Store with provenance (source, fetched_at, quality flags)
    -> Conditionally enqueue lightweight intraday analysis
    -> Run the full end-of-day pipeline after close
```

Run all three processes for full local operation:

```bash
# 1. Web server
python manage.py runserver

# 2. Worker — actually executes tasks (listens to every queue; see "Queues" below)
celery -A config worker -l info -Q market-fast,market-analysis,market-heavy,notifications,celery

# 3. Beat — fires the schedule below (run exactly one of these, ever)
celery -A config beat -l info
```

Without a running worker, `/pipeline/` still enqueues successfully (the job
sits queued in Redis) but nothing executes it until a worker starts. The
`run_market_pipeline` management command is unaffected — it calls the
service functions directly, not through Celery, so it still runs
synchronously from the CLI without a worker.

### Configuration (`.env`)

All in seconds unless noted; every flag can be disabled independently.
Validated at settings-import time (`config/settings/base.py`) — a zero,
negative, or non-numeric interval raises `ImproperlyConfigured` immediately
at process startup, not silently at the first scheduled tick.

```dotenv
AUTO_MARKET_SYNC=True             # live quote sync, on/off
AUTO_SYNC_INTERVAL_MARKET=200     # seconds between fetches while DSE is open
AUTO_SYNC_INTERVAL_OFF=3600       # seconds between freshness checks while closed

AUTO_INTRADAY_ANALYSIS=True       # lightweight technical-snapshot refresh, on/off
AUTO_INTRADAY_ANALYSIS_INTERVAL=900  # minimum seconds between intraday passes

AUTO_DAILY_APPEND=True            # scheduled 10:05/14:05 OHLCV append, on/off
AUTO_ANALYZE_AFTER_APPEND=True    # run full analysis right after a successful append
AUTO_CLOSE_LEARN=True             # forecast settlement + next-session forecast, on/off
AUTO_PAPER_TRADING=True           # admin-only virtual trading; no broker transactions
AUTO_PAPER_TRADING_INTERVAL=900   # seconds between decisions from 10:00 until 14:25

AUTO_ML_TRAINING=True             # daily model retrain, on/off
AUTO_ML_TRAINING_TIME=00:30       # 24h HH:MM, Asia/Dhaka — well before the 10:00 market open

ENABLE_DSE=True
ENABLE_CSE=False                  # CSE work is never scheduled/executed while this is False
```

`AUTO_ML_TRAINING_TIME` is validated as a real 24h `HH:MM` at settings-import
time (`config/settings/base.py`). Training fires every calendar day at that
time — always outside the 10:00–14:30 Asia/Dhaka trading session regardless
of the day of week, so there's no weekday restriction to configure.
`market.services.ml_model.train_model()` itself is a no-op (`{"ok": True,
"skipped": "no new label-resolvable data since last train"}`) when nothing
new has arrived since the last successful run, so a quiet day just logs a
skip rather than doing wasted work.

### Beat schedule

| Task | Schedule | Governing flag |
|---|---|---|
| `market.tasks.sync_live_market` | every 60s tick, self-throttles to `AUTO_SYNC_INTERVAL_MARKET`/`_OFF` | `AUTO_MARKET_SYNC` |
| `market.tasks.run_intraday_analysis` | every 60s tick, self-throttles to `AUTO_INTRADAY_ANALYSIS_INTERVAL` | `AUTO_INTRADAY_ANALYSIS` |
| `market.tasks.append_daily_bars` | 10:05 & 14:05, Sun–Thu | `AUTO_DAILY_APPEND` (+`AUTO_ANALYZE_AFTER_APPEND`) |
| `market.tasks.close_learn_settlement` | 14:45, Sun–Thu | `AUTO_CLOSE_LEARN` |
| `market.tasks.assess_ml_reliability` | 15:20, Sun–Thu | — |
| `market.tasks.run_paper_trading` | every 60s tick; operates 10:00–14:25 Sun–Thu and self-throttles to `AUTO_PAPER_TRADING_INTERVAL` | `AUTO_PAPER_TRADING` |
| `notifications.tasks.send_daily_digest` | 15:00, Sun–Thu | — |
| `market.tasks.sync_pe_ratios` | 10:10, Sun–Thu | — |
| `market.tasks.sync_holiday_calendar` | 28th–31st, 23:30 (self-filters to the true last day) | — |
| `market.tasks.train_ml_model` | daily, at `AUTO_ML_TRAINING_TIME` — **not added to the schedule at all** when `AUTO_ML_TRAINING=False` | `AUTO_ML_TRAINING` |
| `notifications.tasks.send_ml_daily_report` | every 60s tick, self-throttles to once per day at `TELEGRAM_ML_REPORT_TIME` in `TELEGRAM_ML_REPORT_TIMEZONE` — see "Telegram ML daily report" below | `TELEGRAM_ML_DAILY_REPORT` |

Every disabled flag is honored *inside the task itself* too (not just by
omitting a beat entry), so a manual trigger or a stray direct call can't
bypass it. A disabled/skipped run is recorded as `TaskStatus.SKIPPED`, not
`SUCCESS` or `FAILURE` — see "Task status" below.

### Lightweight vs. full analysis

`market.tasks.run_intraday_analysis` (new) only refreshes each stock's
`TechnicalSnapshot` (moving averages, RSI, MACD, Bollinger, ATR — a single
pass over already-fetched price history, no network call, no ML, no pattern/
prediction recompute) for stocks with newer data than their last snapshot.
It never writes a new `AnalysisResult` row, so a stream of small intraday
price ticks never produces a stream of near-duplicate signal rows. Full
analysis (`market.tasks.run_full_analysis`, patterns + predictor + optional
ML blend + `AnalysisResult` upsert) still runs once per session, after the
final daily append.

### End-of-day pipeline

`market.tasks.run_end_of_day_pipeline` (new) runs the same four stage
functions the daily beat schedule already runs — `append_daily_bars` ->
`close_learn_settlement` -> `assess_ml_reliability` -> `send_daily_digest` —
back to back, on demand (the Admin Panel's **Run end-of-day pipeline**
button) or as a single task. Each stage is still independently recorded as
its own `TaskRun` (calling a `@shared_task` function directly, not via
`.delay()`, still runs it through its own `@record_task_run` decoration), so
a failed stage shows up on its own and never silently marks the whole
pipeline "ok" — later stages still run since each reads its own
prerequisites from the database, not from the previous stage's return value.

### Queues, locking, and concurrency

Four named queues (`config/settings/base.py`'s `CELERY_TASK_ROUTES`):
`market-fast` (quote sync — must never queue behind heavy work),
`market-analysis` (intraday analysis, daily append), `market-heavy` (full
analysis, ML training, reliability assessment), `notifications` (digests,
feedback notifications). **A worker must be started with `-Q` naming all
four** (see the command above) — Celery only consumes the queue(s) it's told
to, and `CELERY_TASK_DEFAULT_QUEUE=market-fast` means an unrouted task would
otherwise silently never run on a worker that isn't told about the other
three.

Locking reuses the project's existing infrastructure — no second locking
system:
- `market.services.autosync.exclusive_db_write` (thread lock + Redis-backed
  cross-process lock) already serializes every market-writing task.
- Full analysis and ML training additionally take their own non-blocking
  `market.services.locking.distributed_lock` (`"full-analysis"` /
  `"ml-training"`) *before* attempting the blocking DB-write lock, so a
  genuine duplicate (a manual click while the scheduled run is already
  going) comes back `{"ok": True, "skipped": "already_running"}` —
  recorded as `TaskStatus.SKIPPED` — instead of queueing behind it and
  eventually timing out as a `FAILURE`.
- Redis locks auto-expire (`timeout=`) so a crashed worker can't wedge a
  lock forever.

Conservative defaults for a small VM (documented target: **Oracle Cloud A1,
2 OCPUs, ~20 users**) — override any of these via env if you scale up:

```dotenv
CELERY_WORKER_CONCURRENCY_LIGHT=2
CELERY_WORKER_CONCURRENCY_HEAVY=1
CELERY_WORKER_PREFETCH_MULTIPLIER=1
CELERY_WORKER_MAX_TASKS_PER_CHILD=200
CELERY_TASK_TIME_LIMIT=600
CELERY_TASK_SOFT_TIME_LIMIT=540
ML_MAX_WORKERS=2
```

Production (`docker-compose.yml`) runs **two** worker processes for real
resource isolation, not one: `celery-worker-light` (`-Q
market-fast,market-analysis,notifications,celery`, concurrency 2) and
`celery-worker-heavy` (`-Q market-heavy` only, concurrency 1) — a live-quote
sync or digest on the light worker never queues behind a full-analysis/ML
training run on the heavy one, and the heavy worker's concurrency stays at 1
so it never runs two CPU-heavy jobs at once (`ML_MAX_WORKERS` bounds each
individual job's own thread count separately — see `config/settings/base.py`).
The local dev quickstart above still uses one worker listening to every
queue for simplicity; only production needs the split.

### Task status

`TaskRun.status` (`market/models.py`) now has five values:
`started` / `success` / `partial` / `skipped` / `failure`. A task function
that returns `{"ok": True, "skipped": "<reason>"}` is recorded `SKIPPED`
automatically (`market.services.task_status.record_task_run` inspects the
returned dict); a returned `{"partial": True, ...}` is recorded `PARTIAL`.
This is what lets "market closed, nothing to do" and "disabled by
configuration" show up honestly on the Admin Panel instead of inflating a
"success" count or tripping a failure-streak alert.

### Manual controls (Admin Panel)

Every button enqueues the same task automation uses — no duplicated
business logic. All are POST + CSRF-protected, Admin-only
(`accounts_decorators.admin_required`; Staff/User get `403`), and return
immediately with the job queued:

- **Fetch DSE now** — `market.tasks.fetch_all_market_data` (quotes only, no history)
- **Run lightweight analysis now** — `market.tasks.run_intraday_analysis`
- **Run full analysis now** — `market.tasks.run_full_analysis`
- **Run end-of-day pipeline** — `market.tasks.run_end_of_day_pipeline`
- **Train experimental model** — `market.tasks.train_ml_model`, requires an
  explicit confirmation checkbox/dialog before it's queued
- **Retry** (next to any recent failure) — `POST /pipeline/retry/<task_run_id>/`,
  re-enqueues the same task by name (checked against a fixed allow-list, never
  by replaying stored arguments) — only a `FAILURE`-status run can be retried

Every underlying task already has its own locking, so clicking twice (or a
manual click racing the schedule) is `Skipped`, not duplicated or queued
twice.

### ML-training policy

- Daily, at a fixed off-hours time, never intraday, never because one new
  quote arrived.
- Only one training run at a time (`"ml-training"` lock, see above).
- DSE-only when `ENABLE_CSE=False` — `market.services.ml_model.train_model`
  already restricts training panels to `enabled_exchanges()`.
- Uses the existing chronological walk-forward validation, embargo, and
  deployment gate (non-positive out-of-sample skill vs. baseline is never
  activated) — unchanged by this work.
- The currently active model stays active until a new candidate's skill
  qualifies; a failed or non-qualifying candidate is recorded (`TaskRun`
  `FAILURE`/the trained-but-inactive `MLModelVersion` row) but never swapped
  in for the live one.
- Training never blocks a web request — it's Celery-only.

### Monitoring — Admin Panel "Automation" section

Shows: automation on/off per flag, DSE/CSE enabled, market open/closed,
last successful/failed run per stage (fetch, intraday analysis, full
analysis, daily append, forecast settlement, ML training, reliability
assessment), currently-running tasks, recent failures (with a one-click
Retry), the currently active ML model version, and the daily training
schedule. Backed by `market.services.automation_status.
automation_status_snapshot()` — read-only aggregation over `TaskRun`/
`MLModelVersion`/settings, no secrets (no Redis URL, no broker credentials,
no tokens) ever included.

### New operational alerts

On top of the existing stale-data/repeated-failure/stuck-job/model-
degradation alerts (`market.services.ops_alerts`):

- **Missing daily append** — no successful `append_daily_bars` run by
  15:00 Asia/Dhaka on a trading day. Silent when `AUTO_DAILY_APPEND=False`
  or today isn't a trading day — never alerts for intentionally disabled
  automation or a normal non-trading day.
- **Worker absence** — no `sync_live_market` run in the last 10 minutes
  *during market hours* (that task ticks unconditionally every ~60s
  regardless of session state, so a longer silence means the worker/beat
  process itself is down). Silent outside market hours.
- **Task backlog** — more than 20 items still queued (not yet started)
  across all four Redis queues. Best-effort (a broker connectivity problem
  just yields "nothing to report" here, not a crash — that's a separate,
  existing alert).

None of these fire for: market closed, a holiday, disabled CSE, intentionally
disabled automation, a duplicate task that was correctly Skipped, or "no data
because no trading was expected."

### Eager mode — tests only

`CELERY_TASK_ALWAYS_EAGER=True` (env var, or `@override_settings(...)` per
test) makes `.delay()` run a task's body immediately in-process instead of
enqueueing it. Used by feedback's notification tests, which need the
notify-on-submit/status-change task to actually run inline. **Do not** set
it for the running web server or worker — that reintroduces request-thread
blocking, the exact problem enqueueing exists to avoid.

## Telegram ML daily report

One consolidated, plain-language ML status message to a single configured
Admin, once a day — for a non-technical project owner, not a metrics dump.
Reuses the project's existing Telegram sender (`notifications.services`),
ML reliability calculations (`market.services.reliability_report`/
`reliability_metrics`), and locking/task-status infrastructure — no second
scheduler, no second Telegram client, no duplicated statistics.

### Setup

1. Create a bot via [@BotFather](https://t.me/BotFather) on Telegram
   (`/newbot`) and copy the token it gives you into `TELEGRAM_BOT_TOKEN`.
2. Get your numeric Admin chat id **without exposing it in any web
   request**: message your new bot once, then open
   `https://api.telegram.org/bot<token>/getUpdates` in a browser (replace
   `<token>`) and read `message.chat.id` from the JSON response. Put that
   number in `TELEGRAM_ADMIN_CHAT_ID`.
3. Set the vars below and restart. Never commit real values — `.env`/
   `.env.docker`/`.env.production` are gitignored for this reason.

### Configuration (`.env`)

```dotenv
TELEGRAM_BOT_TOKEN=              # shared with the daily digest below
TELEGRAM_ADMIN_CHAT_ID=          # separate from TELEGRAM_CHAT_ID — never taken from a browser request

TELEGRAM_ML_DAILY_REPORT=True    # independently disables just this report
TELEGRAM_ML_REPORT_TIME=17:00    # 24h HH:MM
TELEGRAM_ML_REPORT_TIMEZONE=Asia/Dhaka
```

`TELEGRAM_ML_REPORT_TIME`/`_TIMEZONE` are validated at settings-import time
(`HH:MM` format, and a real IANA timezone name via `zoneinfo`).
`TELEGRAM_ML_DAILY_REPORT=True` alone doesn't guarantee a real send — without
both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ADMIN_CHAT_ID` set, the report still
generates and is previewable in the Admin Panel every day, it just never
calls Telegram (same graceful-degradation pattern as the existing daily
digest). This doubles as the project's safe development/test mode: run
locally with no token configured and use **Preview today's report** to see
exactly what would be sent.

### Schedule

`notifications.tasks.send_ml_daily_report` ticks every 60s via Celery Beat
(same self-throttling pattern as `run_intraday_analysis` — chosen
specifically because `TELEGRAM_ML_REPORT_TIMEZONE` is independently
configurable from `CELERY_TIMEZONE`, and this Celery version's `crontab`
has no per-entry timezone of its own) and only actually generates/sends once
the current time in `TELEGRAM_ML_REPORT_TIMEZONE` has reached
`TELEGRAM_ML_REPORT_TIME`, at most once per (recipient, calendar date). A
weekend or market holiday still produces a short report — closure isn't
treated as an error, just as "no new evidence today."

### Duplicate prevention

Idempotency key: `telegram_ml_daily_report:<sha256(chat_id)[:16]>:<date>`
(the recipient is hashed, never stored raw — see
`notifications.models.mask_recipient`/`MlDailyReportDelivery`). A retry
after confirmed delivery (`status=sent`) is a no-op; a retry after a
partial/failed send picks back up using the stored `chunks_sent` count
rather than resending what already went out. Force-resending a confirmed
duplicate is a separate, explicit, audited Admin action (see below) — never
the default behavior of a retry.

### Historical vs. live precision

Two numbers, always kept separate, never averaged together:

- **Historical precision** — out-of-sample walk-forward test-fold precision,
  read from `MLModelVersion.metrics` (recorded once, at training time).
  "When the model suggested a rise, it was correct about N times out of
  100" — this is the model's track record on data it never trained on,
  not training-set performance.
- **Live precision** — precision over settled real-world predictions, read
  from the latest `ReliabilityAssessment` (recomputed daily as more
  predictions settle — the existing 15:20 Asia/Dhaka reliability-assessment
  task, not a new calculation). Only counts predictions whose real outcome
  is already known.
- A separate **directional result** ("correct in X out of Y completed
  predictions") is reported alongside live precision but never labeled
  "precision" — it's the plain accuracy of the predicted direction, not
  restricted to rise-predictions.

### Evidence levels

Translates the *live settled* sample size into a plain label — never a bare
count, never a claim of proof:

| Settled predictions | Label |
|---|---|
| 0 | No evidence |
| 1–29 | Very limited |
| 30–99 | Limited |
| 100–299 | Moderate |
| 300+ | Stronger evidence |

A precision percentage is never shown as `0%` merely because there were no
qualifying observations (e.g. the model never predicted a rise in the
window) — that case is reported in words instead.

### Status and recommendations

`market.services.ml_daily_report.determine_status`/`generate_recommendations`
are pure, deterministic functions over stored evidence (deployment status,
`ReliabilityAssessment.status`, sample size, calibration error, data-quality
alerts, whether a newer candidate failed the deployment gate) — never an
LLM-generated verdict. Status is one of: No evidence / Experimental / Weak /
Promising / Stable / Declining / Suspended. Up to 3 recommendations, most
urgent first, each mapped from an actually-detected condition (e.g. "too few
settled predictions" → collect more evidence; "declining live performance" →
pause promotion and investigate; never "always retrain").

### Manual controls (Admin Panel → Telegram ML Report)

- **Preview today's report** — renders (read-only, no Celery, no network
  call) exactly what would be sent right now.
- **Send / retry today's report** — enqueues the same task automation uses;
  respects the once-per-day idempotency guard automatically, so this is
  also how you retry a failed delivery.
- **Force resend** — explicit confirmation dialog, bypasses the idempotency
  guard, and is recorded in `AdminAuditLog`
  (`AdminAuditAction.TELEGRAM_REPORT_FORCED`).

All three are Admin-only (`403` for Staff/User), and the send action is
POST + CSRF-protected. The recipient is never accepted from the request body
— it always comes from `settings.TELEGRAM_ADMIN_CHAT_ID`.

### Failure handling

Transient errors (network failure, timeout, Telegram rate limiting/5xx) are
retried with backoff + jitter (bounded, `max_retries=5`) via Celery's
`autoretry_for`; permanent errors (bad token, unknown chat, malformed
request) are recorded `Failed` immediately, not retried indefinitely. Three
consecutive failed report dates raise a deduplicated in-app Admin
operational alert (`market.services.ops_alerts`) — shown on the Admin Panel,
never re-sent through the same (evidently broken) Telegram channel it's
reporting on. A failed send never touches `MLModelVersion`,
`ReliabilityAssessment`, or `PredictionSnapshot` — report generation is
strictly read-only.

### Disabling

Set `TELEGRAM_ML_DAILY_REPORT=False` and restart — this only disables the
daily report; the daily digest (`TELEGRAM_CHAT_ID`) and per-user Telegram
alerts are unaffected. There's no runtime (no-restart) toggle — like every
other `AUTO_*`/`TELEGRAM_*` flag in this project, it's an env var read at
process startup.

## Testing & coverage

```bash
# Run the full suite (Redis must be running — some locking tests use it directly)
python manage.py test accounts api market notifications

# Portfolio feature only
python manage.py test market.tests.test_portfolio

# Exchange feature flags (DSE-only mode) only
python manage.py test market.tests.test_exchange_config

# Roles & permissions (see "Roles & permissions" above) only
python manage.py test accounts market.tests.test_navbar

# CI-friendly: run under coverage and fail the build below the configured gate
coverage run manage.py test accounts api market notifications
coverage report          # terminal summary, fails if TOTAL < fail_under (.coveragerc)
coverage html             # optional: htmlcov/index.html
```

Both commands above run under `config.settings.development` (the default —
see "Quick start"), which is what this whole suite has always been
validated against. `config/settings/test.py` also exists (fixed dummy
secret key, fast password hasher) for CI setups that want a settings
module that never reads a local developer `.env`. It deliberately does
**not** touch Celery eager-mode or the `AUTO_*` flags — an earlier draft
did, and it silently broke `test_task_idempotency.py` (which exercises
Celery's own default eager-retry behavior and assumes `AUTO_MARKET_SYNC`
is on) until caught by running the full suite under it:
`DJANGO_SETTINGS_MODULE=config.settings.test python manage.py test accounts api market notifications`
— both are covered by `market/tests/test_settings.py`, and the full suite
passes under either.

Config lives in `.coveragerc`. The `fail_under` gate (currently 45%) is a
realistic floor for the *whole* project, not the target — it's pulled down by
modules with real network orchestration that is deliberately not exercised
end-to-end (fetch *parsing* is fixture-tested; the network calls themselves
are mocked so tests never hit a live endpoint). The **critical business-logic
modules already meet or exceed 70%**: indicators (100%), models incl.
price-history (93.7%), predictor (80.1%), screener (91.8%), backtest (93.5%),
api/serializers (100%), api/views (90.2%), `ml_model.py` (77.9%),
`ml_training.py` (86.1%), and the ML Reliability Monitor's
`reliability_report.py` (96.1%), `reliability_metrics.py` (93.6%),
`reliability_settlement.py` (91.7%), `reliability_capture.py` (83.9%),
`reliability_drift.py`/`reliability_recommend.py` (83.3%). Remaining low-coverage areas (`close_learn.py`'s
older forecast/backfill code paths, `dse_fetcher.py`/`cse_fetcher.py`
orchestration beyond parsing, `market/views.py` page views, `autosync.py`
status helpers) are the roadmap for future passes — tracked, not silently
ignored.

All tests use Django's isolated in-memory test database. Tests that exercise
real model training (`market/tests/test_ml_training.py`) redirect the model
file path to a `tempfile.TemporaryDirectory()` via `mock.patch.object`, so
even full train_model()/train_next_close_model() runs never touch
`data/cache/*.pkl`. The real `db.sqlite3` is likewise never touched by
`manage.py test` — verified via MD5/mtime comparison before and after test
runs.

## Model training & evaluation (Phase 4)

Two RandomForest models refine (never replace) the rule-based predictor:
`forward_return_rf` (probability a stock's close is higher 10 trading days
out) and `next_close_rf` (predicted next-session close return, feeding the
after-close "learn loop"). Both are trained by
`market/services/ml_training.py` + `ml_model.py` / `close_learn.py` under
these rules:

- **Chronological, validated input.** Every stock's price history is
  sorted by date and rejected (`ValueError`) if it contains a null or
  duplicate date, before any feature is computed.
- **No fabricated labels.** A row whose forward-looking target isn't known
  yet (within the last N trading days of a stock's history) is dropped, not
  assigned the "no gain" class — see
  `market/tests/test_ml_training.py::ForwardReturnLabelLeakageTests` for the
  regression test against this exact bug.
- **Chronological walk-forward CV, never a random split.** Folds are
  date-ordered and expanding; an embargo gap (≥ the forecast horizon)
  separates the end of each train fold from the start of its test fold, so
  no training label's forward-looking window can reach into test data.
  Preprocessing (median imputation) is fit on the train fold only.
- **DSE and CSE are evaluated separately by default.** They're only pooled
  into one combined deployed model when both have enough out-of-sample rows
  (≥200/300) *and* their walk-forward skill is within 0.15 of each other —
  otherwise two separate model files are trained and deployed
  (`forward_return_rf_DSE.pkl` / `_CSE.pkl`), each serving only its own
  exchange. The `combine_justified` flag + reason is recorded on every
  training run.
- **Baselines**: majority-class, zero-return ("predict no change"),
  persistence (own recent momentum/return continues), and a simple-market
  baseline (train-fold mean/majority). Reported per-fold and aggregated
  alongside accuracy, balanced accuracy, precision/recall, Brier score
  (classifier) or MAE/MAPE (regressor), and direction hit rate.
- **Deployment gate.** A trained artifact is only marked `active` if its
  aggregated walk-forward skill vs. the majority-class/zero-return baseline
  is strictly positive; otherwise it's saved `experimental` for audit and
  `ml_probability()` / `_ml_one_day_return()` refuse to serve it — inference
  silently falls back to the pure rule-based score, so an unproven model can
  never produce a "confident" (score-shifting) recommendation. The gate is
  re-checked against `MLModelVersion.is_active` in the database at
  *inference* time (not just the pickle's own snapshot), so
  `next_close_rf` can also be downgraded automatically if its **live**
  settled-forecast skill (`compute_skill_metrics`) turns non-positive after
  ≥50 real settlements, without waiting for the next scheduled retrain.
- **Versioning & backups.** Every training run writes a `MLModelVersion` row
  (version tag, exchange scope, status, data cutoff, feature schema, train
  row count, per-fold dates/sample-counts/class-balance, full metrics incl.
  baselines) — visible in Django admin. The previously deployed `.pkl` is
  copied to `data/cache/backups/` (never deleted) before being overwritten,
  so a bad retrain is always recoverable by hand.

Retrain + print a walk-forward report:

```bash
python manage.py train_ml_models              # both models, all exchanges
python manage.py train_ml_models --limit 150  # stocks/exchange used for training
python manage.py train_ml_models --json       # full raw result incl. all fold metadata
```

Walk-forward metrics are estimates from a limited BD-market lookback window,
not guarantees of future performance — treat "active" as "beat a naive
baseline on this historical sample," not as investment advice.

## Backtesting (Phase 5)

`market/services/backtest_engine.py` is a shared-cash **portfolio**
backtester (`market/services/backtest.py` is a thin persistence wrapper
around it) — replacing an older single-stock, cost-free simulator whose
runs are kept as `BacktestRun.engine_version="v1"` rows (never deleted;
new runs are `"v3"` and always **insert** a new row rather than overwrite
one by name, so every run is a permanent, auditable historical record).

```python
from market.services.backtest import run_backtest
from market.services.backtest_engine import CostConfig, PortfolioConfig
from market.models import Exchange

run = run_backtest(
    name="DSE 2y sample", strategy="rsi_macd_v1", exchange=Exchange.DSE,
    start_date=..., end_date=...,
    cost_config=CostConfig(),          # brokerage/tax/spread/slippage/liquidity — see defaults below
    portfolio_config=PortfolioConfig(),  # cash, position sizing, max concurrent positions, hold window
)
```

**Point-in-time correctness.** Signals are read off a stock's indicator
row for day *d* only (indicators are purely causal rolling computations).
Every order resulting from a day-*d* signal executes at the **open of that
stock's own next trading session** — never the signal bar's own close.
Two stocks with identical histories up to a point and different future
paths afterward produce identical signal/entry dates and prices — see
`market/tests/test_backtest.py::LookaheadPreventionTests`.

**Costs** (`CostConfig`, percent of trade notional unless noted — illustrative
BD retail-brokerage approximations, not tax/legal advice):

| | default |
|---|---|
| Brokerage | 0.30% |
| Tax/AIT | 0.05% |
| Bid-ask spread | 0.10% (modeled as an adverse price move) |
| Slippage | 0.10% (modeled as an adverse price move) |
| Liquidity limit | 5% of that session's traded volume per trade |
| Min session volume to trade | 100 shares (else treated as suspended/untradeable) |

**Portfolio** (`PortfolioConfig`): BDT 1,000,000 starting cash, 5% of
current equity per new position, up to 20 concurrent positions, 20-session
hold with an early exit at +5%. Cash and per-session liquidity are strictly
enforced — a signal is sized down or rejected outright rather than ever
letting cash go negative or a single trade consume more than the liquidity
cap; the run's `warnings` field records every rejection/size-down/forced
close, and overlapping signals (a fresh signal while already holding that
stock, or while `max_positions` is full) are counted separately.

**Missing sessions & suspensions.** A pending order retries at the stock's
own next session (up to 5 attempts) before being abandoned (buys) or kept
retrying (sells, which are force-closed at the last known price only if
the backtest window ends first). **Corporate actions**: this database has
no adjusted-close/split data source. Any trade whose entry or exit lands
on a day with an implausible single-day return (>=25%) is flagged and
**excluded from headline metrics** (reported separately as
`corporate_action_excluded_trades`) rather than silently treated as a real
25%+ move.

**Benchmarks**: equal-weight buy-and-hold of the same tested stock universe,
and an equal-weight daily-return index *proxy* (no official DSEX/CSCX level
is stored in this database — `MarketSnapshot` has only a handful of rows).

**Metrics**: total/annualized return, max drawdown, Sharpe & Sortino
(250 trading-days/year, BD's Sun–Thu week), profit factor, win rate,
avg win/loss, turnover, total costs, average exposure, and trade count —
plus a `breakdown` by year/exchange/sector, each flagged with a small-sample
warning below 20 trades.

**Data safety**: a full `db.sqlite3` backup is taken before any migration
(`data/backups/`); the Phase 5 migration only *adds* columns to
`BacktestRun` — no existing row or column was altered or dropped.

## Data provenance & quality (Phase 6)

Every `PriceHistory` row now carries: `source` (`dse_live`/`dse_history`/
`cse_live`/`cse_history`/`cse_mirror_fallback`/`synthetic_demo`/
`synthetic_fallback`/`unknown`), `is_synthetic`, `fetched_at`,
`adjustment_status` (this database has no corporate-action data source, so
every real write is `raw`, never `adjusted`), `import_batch` (FK to
`ImportBatch` — one row per fetch/seed run, with bounded troubleshooting
metadata and never any secret/header/cookie/token), and `quality_flags`
(a list of detected issues, never used to reject a write).

**Backfill**: all 635,076 pre-Phase-6 rows were migrated to
`source="unknown"` / `is_synthetic=False` / `fetched_at=None` — provenance
was never tracked before, and there's no reliable way to reconstruct it
after the fact (a `seed_demo_universe()` write and a later real fetch both
go through the same upsert path and are indistinguishable in retrospect),
so legacy rows are honestly labeled unknown rather than guessed as live.
`PriceHistory.objects.live()` (the default for analysis/training/backtests)
excludes only rows *positively confirmed* synthetic — treating the entire
legacy dataset as suspect would make the product unusable, and "unknown"
is not evidence of being fake.

**Validation is advisory, not blocking** (`market/services/data_quality.py:validate_ohlcv`):
positive prices, non-negative volume, and `low <= open/close <= high` are
checked on every write, but a violation is *flagged*, never rejected —
real DSE/CSE feeds turned out to already contain ~154k such rows (see the
scan results below), and silently dropping them on a hard constraint would
be exactly the "silent discard of evidence" this phase was asked to avoid.
The one true DB-level enforcement is unchanged from Phase 1: `unique(stock,
date)`.

**Quality scan** (`market/services/data_quality.py:run_quality_scan`, also
`python manage.py data_quality_scan`): walks each stock's full history and
flags impossible OHLC, abnormal single-day jumps (>=25%, almost always an
unadjusted split rather than organic trading), stale-quote runs (5+
identical closes with nonzero volume — a stuck/duplicated feed), and
missing-session gaps (>10 calendar days between consecutive bars). Flags
are recomputed (not accumulated) on every rescan and never delete a row.

**Demo isolation**: `analyze_stock()`, `run_full_analysis()`,
`build_training_panel()` (both ML models), `run_backtest()` and every
close-learn training/context query default to `.live()` — synthetic/demo/
mirror-fallback data is excluded unless `include_demo=True` is passed
explicitly. `save_history()` additionally refuses to let a synthetic write
overwrite a row that already has a confirmed-real source, and any write
that materially changes an existing row's OHLCV first snapshots the prior
values to `PriceHistoryRevision` — an overwrite is never a silent loss.

**Staff report**: `/data-quality/` (linked from the nav, staff-only) shows
row counts by source, unknown/synthetic/flagged counts, per-exchange
freshness, and recent `ImportBatch` health (success/error). CLI equivalent:
`python manage.py data_quality_scan --report-only`.

## Deployment (Phase 8)

Full procedure, required env vars, and rollback steps:
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md). Summary:

- **Settings split**: `config/settings/{base,development,test,production}.py`.
  `manage.py`/`wsgi.py`/`asgi.py`/`celery.py` default to `development` —
  unchanged local behavior. Production requires
  `DJANGO_SETTINGS_MODULE=config.settings.production` set explicitly.
- **Fail-fast config**: `production.py` raises `ImproperlyConfigured` at
  import time if `SECRET_KEY` (weak/placeholder/missing), `ALLOWED_HOSTS`,
  `CSRF_TRUSTED_ORIGINS`, any `POSTGRES_*`, or `CELERY_BROKER_URL` is
  missing — the process refuses to start rather than serving with an
  insecure default.
- **HTTPS/headers**: HSTS (1y + subdomains + preload), secure
  session/CSRF cookies, `X-Frame-Options: DENY`, content-type nosniff,
  `same-origin` referrer policy, SSL redirect behind a proxy that sets
  `X-Forwarded-Proto`. `development.py`/`test.py` explicitly turn these
  back off (documented local-HTTP exception — see `docs/DEPLOYMENT.md`).
- **Database**: PostgreSQL via `POSTGRES_*` env vars in production;
  SQLite unchanged in development/test. The SQLite→Postgres migration is
  a documented **manual** procedure (backup → `dumpdata` → `migrate` on
  empty Postgres → `loaddata` → verify row counts → cut over) — nothing
  runs it automatically.
- **Celery/Redis**: `rediss://` (TLS+auth) required unless
  `CELERY_BROKER_ALLOW_PLAINTEXT=True` is set explicitly; task time
  limits, worker recycling, and broker connection timeouts configured
  via env.
- **Static files**: WhiteNoise serves `collectstatic` output directly
  from the app process (no new external service). Deploy step:
  `python manage.py collectstatic --noinput`.
- **Dependencies**: `requirements.txt` (direct, pinned) and
  `requirements-lock.txt` (full transitive closure — production installs
  from this). Scanned with `pip-audit`: 0 known vulnerabilities as of
  2026-07-30.
- **Deploy check**: `DJANGO_SETTINGS_MODULE=config.settings.production python manage.py check --deploy`
  → `System check identified no issues (0 silenced)` against a
  fully-populated `.env.production.example`-shaped environment.

This phase does not provision, deploy to, or modify any external/live
infrastructure — it prepares the settings, docs, and dependency pins a
human operator needs to do that themselves.

## Exchange feature flags

Bazaar can run as a DSE-only deployment to reduce operational complexity
(fewer upstream dependencies to babysit, no CSE-specific scraping quirks
to monitor) while keeping every line of CSE-related code, every CSE
`Stock`/`PriceHistory`/`AnalysisResult`/`PortfolioTransaction` row, and
the ability to turn CSE back on at any time. **Disabling CSE is an
operational configuration change, never data deletion.**

### Settings

```text
ENABLE_DSE=True    # default True
ENABLE_CSE=False   # default False for a fresh deployment
MAINTENANCE_MODE=False
```

Read from the environment by `config/settings/base.py` (common truthy
spellings — `1`/`true`/`yes`, case-insensitive — are accepted, matching
every other boolean flag in this project's settings). `development.py`
and `test.py` both re-enable CSE on top of that default specifically so
local `runserver`/`manage.py test` keep exercising both exchanges exactly
as they did before this flag existed — set `ENABLE_CSE=False` in your own
`.env` to test DSE-only mode locally. A fresh production deployment with
no override is DSE-only by default.

If both `ENABLE_DSE` and `ENABLE_CSE` end up false, the process refuses
to start (`ImproperlyConfigured` at settings-import time) — a
deployment serving zero exchanges is treated as a misconfiguration
unless `MAINTENANCE_MODE=True` explicitly acknowledges a deliberate
full-outage window (e.g. planned database maintenance).

Every module that needs to know which exchanges are active calls
`market.services.exchange_config.enabled_exchanges()` /
`is_exchange_enabled(exchange)` rather than reading these settings or
`os.environ` directly — this is the single place the policy is
expressed, and it's a pure function of settings (no DB/network access,
safe to call anywhere including at import time).

### What disabling CSE does

- **Public discovery** (stock list/detail, screener rankings, dashboard
  summaries, watchlist-add, new prediction/analysis requests, the CSE
  ticker rail and market-status chip) excludes CSE entirely, enforced
  server-side — not just hidden with CSS. A direct request for a CSE
  stock/analysis/prediction route (web or API) returns **404**, the same
  as a nonexistent stock, consistently across every such route.
- **Background processing** — live-price fetching, daily-bar collection,
  analysis, forecast generation, reliability settlement, ML training —
  skips CSE. Every fetcher entry point (`sync_cse_live`,
  `sync_cse_history`, etc.) independently no-ops before any network call
  if CSE is disabled, so even a direct/manual call to one of these
  functions can't accidentally hit the network — the guard isn't only in
  the Celery task/orchestration layer. Skips are logged as intentional
  (`"skipped": "exchange_disabled"`), never recorded as a task failure.
- **ML models**: `train_model()`/`train_next_close_model()` never build a
  CSE training panel while disabled, so a "combined" DSE+CSE model can
  never be (re)trained during that window. If a combined model was
  already active before disabling CSE, `load_model()`/
  `load_next_close_model()` bypass it (without deleting or deactivating
  the artifact) and fall back to a DSE-scoped model instead — the next
  successful training run naturally produces a fresh DSE-only artifact,
  since an empty CSE panel can never justify pooling. Existing model
  artifacts and their `MLModelVersion` rows for CSE are left untouched.
- **Operational monitoring**: `market.services.data_quality.provenance_report()`
  tags each exchange's freshness info with `"enabled"`, and
  `ops_alerts._stale_data_alerts()` skips a disabled exchange entirely —
  a deliberately disabled exchange never trips a stale-data alarm,
  missing-batch alert, or false Celery failure. `ops_summary()` includes
  `enabled_exchanges` for staff visibility; the ops report and data-quality
  pages both show an explicit Enabled/Disabled badge per exchange.
- **Management commands**: `fetch_history --exchange both` means *all
  currently-enabled* exchanges, not literally DSE+CSE. Requesting a
  disabled exchange explicitly (`--exchange cse` while `ENABLE_CSE=False`)
  exits safely with an explanation instead of fetching; pass
  `--force-disabled` to override deliberately (e.g. to pre-warm catch-up
  history ahead of re-enabling). `assess_ml_reliability` defaults to
  enabled exchanges but still allows an explicit `--exchange CSE` (a
  read-only diagnostic over already-captured predictions).

### Existing user data (portfolios/watchlists)

A disabled exchange's *public* discovery surfaces are excluded, but a
user's own existing records are never hidden or deleted:

- Existing CSE portfolio holdings and transaction history remain fully
  visible on the portfolio page and via the API, clearly labeled with a
  banner ("CSE support is temporarily disabled…") and a per-holding
  **"Exchange disabled"** quote-status badge — never shown as Live/Delayed/
  Market-closed, and "today's P/L" is suppressed rather than computed
  from stale bars.
- **New CSE BUY transactions are rejected** with a clear message, both
  from the web form and the API (both route through the same
  `market.services.portfolio.create_transaction`/`update_transaction`
  service functions, so the API can't bypass the form-level restriction).
  A corrective **SELL** or position close-out for an existing CSE holding
  is still allowed — it doesn't require live market access. Deleting a
  transaction is always allowed regardless of exchange state.
  "Editing" a transaction is subject to the same BUY-into-a-disabled-
  exchange check as creating one (an edit that would leave/make it a BUY
  for a disabled exchange is refused the same way).
- Removing a CSE stock from a watchlist always works, even though the
  CSE stock's own detail page 404s (the watchlist page itself provides a
  direct remove action so it's reachable regardless).
- Cross-user ownership checks (`_owned_portfolio`/API 404-for-others) are
  completely unaffected by this feature — unchanged code path.

### Re-enabling CSE

1. Set `ENABLE_CSE=True` (env var or `.env`).
2. Restart the web process(es) and all Celery workers/beat — the flag is
   read from Django settings at process start, so a running process
   won't pick up the change without a restart.
3. CSE navigation, ticker, market-status chip, stock discovery, filters,
   watchlist-add, and new-BUY portfolio transactions become available
   immediately on the next request.
4. Scheduled fetching/analysis resumes on its normal cadence (see
   `config/celery.py`'s `beat_schedule`) — no separate re-enable step.
   To force an immediate refresh instead of waiting for the next
   scheduled tick: `python manage.py fetch_history --exchange cse` (fetch
   catch-up history) then `python manage.py run_market_pipeline --analyze`
   (or trigger "Fetch live + analyze" from the staff dashboard).
5. Existing CSE rows are safely reused (all writes are `update_or_create`/
   merge-upsert keyed by `(stock, date)`, not append-only) — catch-up
   fetching after a long gap does not duplicate historical rows.
6. Until fresh data actually lands, existing (now stale) CSE prices keep
   showing their true quote status (Stale/Market closed/etc., computed
   from real timestamps) rather than being presented as current — there
   is no separate "catch-up in progress" state to manage.
7. Portfolio/watchlist associations were never touched while disabled, so
   they're immediately consistent again with no migration step.

### Testing

```bash
python manage.py test market.tests.test_exchange_config
```

Covers: DSE-enabled/CSE-disabled (public pages, portfolio behavior, API,
background tasks, health/ops), both-exchanges-enabled (regression
coverage that disabling logic doesn't affect the default-both-enabled
dev/test configuration), and safety cases (both-disabled startup
validation, boolean env parsing, direct fetcher calls make no network
request, no destructive migration, cross-user protection unaffected).
Uses `@override_settings(ENABLE_DSE=..., ENABLE_CSE=...)` throughout
rather than the developer's local environment.

### Known limitations

- No automatic "catch-up progress" indicator — after re-enabling, a
  manual `fetch_history`/pipeline run (or waiting for the next scheduled
  tick) is what actually refreshes CSE data; the UI doesn't distinguish
  "just re-enabled, not yet refreshed" from "always been enabled".
- `close_learn`'s manual `--backfill` path (a rarely-used, staff-triggered
  historical backfill, not part of the automatic scheduled pipeline) does
  not itself filter by enabled exchange — the automatic forecast/train
  paths do.
- Model degradation alerts for a *pre-existing* CSE model artifact are
  not suppressed by disabling CSE (that artifact's recorded skill is a
  genuine historical fact, independent of the flag) — only the
  data-freshness/stale-data alert class is exchange-flag-aware.

## Operational readiness (Phase 9)

Incident response, rollback, and backup schedule/retention:
[`docs/RUNBOOKS.md`](docs/RUNBOOKS.md). No paid or external monitoring
service is connected — everything below is local (structured logs, two
HTTP endpoints, a staff page, and CLI commands). Summary:

- **Structured logs**: every log line carries a `request_id` (web,
  `market/middleware.py`) or `task_id`/`task_name` (Celery, signal
  handlers in `config/celery.py`) via `config/logging_utils.py`'s
  `ContextVar`-backed filter. Human-readable in dev/test, one JSON
  object per line in production. **Every** formatter redacts
  secret-shaped text (key=value pairs, `Authorization: Bearer ...`,
  credentials embedded in a connection URL) *and* the literal configured
  `SECRET_KEY`/`TELEGRAM_BOT_TOKEN`/`EMAIL_HOST_PASSWORD`/DB password —
  applied to the fully-rendered line, including exception tracebacks, so
  it can't be bypassed by logging `exc_info`.
- **Liveness/readiness**: `/health/live/` never touches a dependency
  (always 200 if the process can route a request at all).
  `/health/ready/` checks the database and Celery broker and returns 503
  with `{"checks": {...: false}}` on failure — booleans only, no
  exception text or connection details in the response (that detail
  goes to the redacted log instead).
- **Ops metrics + alert thresholds**: `market/services/ops_metrics.py`
  (task duration/failure rates, prediction volume + trend, rejected/
  flagged price rows, model drift/evaluation performance — reusing
  Phase 6's `provenance_report()` and Phase 7's `signal_status`) and
  `market/services/ops_alerts.py` (stale data, repeated fetch failure,
  job overlap/stuck jobs, database problems, model degradation). Staff
  page: `/ops/`. CLI: `python manage.py ops_alerts_scan
  --fail-on-critical` (cron/CI-friendly exit code).
- **Admin audit trail**: `AdminAuditLog` (read-only in Django admin —
  add/change/delete all denied) records every pipeline trigger
  (`/pipeline/`) and every staff model-activation/deactivation
  (`MLModelVersion` admin actions: "Deactivate"/"Reactivate selected
  model version(s)", the manual override for a degrading model — see
  the runbook's "Incident: model degradation").
- **Backups**: `python manage.py backup_bazaar --prune-keep 14` writes a
  timestamped `data/backups/<ts>/` (db + ML model artifacts +
  `SHA256SUMS.txt` + `manifest.json` with a row-count snapshot).
  `python manage.py verify_backup data/backups/<ts>` performs an **isolated**
  test restore (never touches the real db/model files) — SHA256 +
  `PRAGMA integrity_check` + row-count comparison against the manifest —
  and only this can honestly claim `RESTORE VERIFIED`. Both were run
  against the real repository database for this phase — see the Phase 9
  report for the exact output.
- **Tests**: 47 new tests (health endpoints incl. a live 503 demo,
  redaction incl. exception tracebacks, admin audit records incl.
  survival across user deletion, backup/restore incl. deliberately
  corrupted/mismatched fixtures) plus follow-up coverage for
  `ops_metrics`/`ops_alerts`/the `/ops/` view.

## ML Reliability Monitor

Ops metrics (above) show whether the pipeline is *running*; this answers
a different question — whether `forward_return_rf` and `next_close_rf`
are still *statistically useful*, using only predictions that were
recorded before their outcomes were known.

**What "reliability" means here.** Every day, once that session's
`AnalysisResult` (classifier) and `NextDayCloseForecast` (regressor) rows
exist, an immutable `PredictionSnapshot` row is captured for each —
predicted value, a rule-based baseline (`predictor.predict_stock`'s
score sign / prior session's return), a naive baseline (majority class /
zero return — the same baselines the training-time deployment gate
itself uses), the exact model version, and a feature-schema hash. Once
written, none of that is ever changed again. Later, once the real
trading session it targets has a price bar, settlement fills in the
actual outcome. A "reliability assessment" then evaluates a rolling
window (30/90/180/365 settled predictions) of one model version's own
snapshots — never mixing across a retrain — and reports a status:

| Status | Meaning |
|---|---|
| `insufficient_data` | Fewer than 30 settled predictions in this window — no claim can be made yet. |
| `healthy` | Positive skill vs. the naive baseline, ≥60 samples, no calibration/drift concerns. |
| `watch` | Positive skill but a mitigating concern (small sample, calibration drift, prediction-distribution drift). |
| `degraded` / `critical` | Skill vs. naive baseline is not positive. |

None of these mean "safe" or "profitable" — see the economic
diagnostics on every assessment (a simple long/flat sanity check at
1x/1.5x/2x `BacktestRun`'s existing cost assumptions, not a portfolio
backtest) before drawing that conclusion.

**Metrics**: accuracy/balanced accuracy/precision/recall/F1/ROC
AUC/Brier/log loss/calibration error + reliability buckets (classifier);
MAE/RMSE/median absolute error/SMAPE/direction accuracy/bias/correlation
(regressor); skill vs. both the rule-based and naive baseline for both.
95% confidence intervals via a seeded i.i.d. bootstrap (documented
limitation: prediction snapshots aren't fully independent, so this
understates true uncertainty vs. a rigorous block bootstrap).

**Drift**: reference = the older half of the current window, current =
the newer half (this project doesn't persist raw training-time feature
distributions, so drift *since training* can't be measured directly —
only drift *since the model went live*). PSI/KS on the prediction
distribution, class-balance/calibration/performance drift between
halves, and a feature-schema diff against what the active
`MLModelVersion` was actually trained on. Crossing a threshold never
triggers an automatic retrain — only a recommendation.

**Run it**: `python manage.py assess_ml_reliability` (`--model`,
`--exchange`, `--window`, `--model-version`, `--json`, `--settle-only`,
`--dry-run` — `--dry-run` makes the whole run read-only, nothing is
captured/settled/persisted). Scheduled daily via the
`assess-ml-reliability-1520` Celery beat entry (`config/celery.py`),
after `train_ml_model`/`close_learn_settlement`/the digest so that
day's prediction artifacts already exist to capture from. Staff
dashboard: `/ml-reliability/`. Staff-only JSON: `/api/ml-reliability/`.

**Investigating a non-healthy status**: see `docs/RUNBOOKS.md`'s
"Incident: ML reliability degradation" — how to read the recommendations
(each cites the exact metric that produced it), recalibrate, retrain,
compare against baselines, and roll back, without ever automatically
activating a new model (any retrained candidate still has to pass the
existing walk-forward evaluation + deployment gate).

**Tests**: `market/tests/test_reliability_*.py`,
`test_assess_ml_reliability_command.py`, `test_ml_reliability_view.py`,
`api/tests/test_ml_reliability_api.py` — capture immutability/dedup,
correct trading-session settlement (holidays, missing prices, suspended
volume, delisted stocks), idempotent settlement/assessment, no
look-ahead, fixed-example metric correctness, single-class handling,
calibration buckets, deterministic bootstrap CIs, drift vs. no-drift
controls, model-version and exchange isolation, cross-exchange
divergence flagging, Celery locking/retry, and staff permissions.

## Release readiness (Phase 10)

Full independent audit, exact evidence, and remaining risks:
[`docs/RELEASE_READINESS.md`](docs/RELEASE_READINESS.md). Verdicts:
Security **PASS**, Reliability **PASS WITH CONDITIONS**, Data quality
**PASS WITH CONDITIONS**, ML validity **PASS**, Backtesting **PASS WITH
CONDITIONS**, UX honesty **PASS**, Operations **PASS**.

**The headline finding**: a fresh, realistic (cost- and benchmark-aware)
1-year backtest run for this audit shows the strategy losing money and
underperforming both the buy-and-hold and index benchmark on both DSE
and CSE over the most recent real period. **This project does not
currently demonstrate a trading edge net of costs, and no part of it
should be presented as investment advice, a safe-buy signal, or
production-ready for real trading.** No order-execution capability
exists in this codebase at all.

## Portfolio

Authenticated users can record their own DSE/CSE holdings as a plain BUY/SELL
transaction ledger and see live-tracked cost basis, market value, and
profit/loss against the same `Stock.last_price`/`PriceHistory` data the rest
of the app uses — no second quote-fetching system, and no page request ever
triggers a live scrape (see "Live price updates" below).

**Nothing is stored pre-calculated.** `Portfolio` and `PortfolioTransaction`
are the only two tables; every holding, cost basis, and P/L figure is derived
on demand from the transaction history by `market/services/portfolio.py`, so
there's nothing that can drift out of sync with the ledger. Every user gets a
default portfolio automatically on first visit (`GET /portfolio/`); a
`UniqueConstraint` enforces at most one `is_default=True` portfolio per user
at the database level, not just in application code.

### Cost basis method: weighted average cost (WAC)

- Every **BUY** adds `quantity × price` to a running purchase-cost total and
  its fee to a running fees total. `cost_basis = purchase_cost + fees` at all
  times.
- Every **SELL** removes a *proportional* slice of both running totals —
  proportional to `quantity_sold / quantity_held_immediately_before` — so a
  partial sale never changes the average price of the remaining shares. The
  removed slice's difference from the sale proceeds becomes that sale's
  contribution to **realized P/L**, which accumulates independently of
  **unrealized P/L** (marked only against the shares still held). Sell-side
  fees reduce proceeds; buy-side fees capitalize into cost basis — standard
  treatment, and the two are never mixed.
- Closing a position completely always leaves `cost_basis` at exactly `0`,
  never a rounding-drifted near-zero, because the final sale's ratio is
  always exactly 1.
- **Formulas**, for one stock within one portfolio, replayed in chronological
  transaction-date order:
  ```
  BUY:  purchase_cost += qty * price;  fees_basis += fee;  qty_held += qty
  SELL: ratio = qty_sold / qty_held
        cost_removed = purchase_cost*ratio + fees_basis*ratio
        proceeds = qty_sold*price - fee
        realized_pl += proceeds - cost_removed
        purchase_cost -= purchase_cost*ratio;  fees_basis -= fees_basis*ratio
        qty_held -= qty_sold

  cost_basis      = purchase_cost + fees_basis
  average_price   = purchase_cost / qty_held                  (excludes fees)
  market_value    = qty_held * latest_price
  unrealized_pl   = market_value - cost_basis
  unrealized_pl_pct = unrealized_pl / cost_basis * 100        (undefined, shown as "—", if cost_basis is 0)
  today_pl        = (latest_price - previous_close) * qty_held
  today_pl_pct    = (latest_price - previous_close) / previous_close * 100
  allocation_pct  = holding_market_value / total_portfolio_market_value * 100
  ```
- A **future-dated** transaction is stored immediately but excluded from
  every "current state" calculation (`transaction_date__lte=today` at the
  query level) until its date actually arrives.
- Every mutation (create/edit/delete) is validated against the transaction's
  own date first, and an edit/delete is additionally re-validated against
  the **entire** post-mutation chronological sequence for that
  (portfolio, stock) — editing an early BUY's quantity down, or deleting it,
  is rejected if a later SELL would then exceed what was actually held,
  wrapped in `transaction.atomic()` so a rejected mutation never partially
  commits.
- All monetary/quantity fields are `DecimalField`; `market.services.portfolio`
  converts any float (e.g. `Stock.last_price`) via `Decimal(str(x))`, never
  `Decimal(x)` directly, so no float binary-imprecision ever enters the math.

### Quote status: Live / Delayed / Stale / Market closed / Demo-Synthetic / Unavailable / Exchange disabled

`quote_status()` never labels a price "Live" just because the page loaded
recently — it's purely a function of the quote's own age, source, and the
exchange's real session state (precedence, first match wins):

1. No price at all → **Unavailable**
2. Stock's exchange is currently disabled (`ENABLE_DSE`/`ENABLE_CSE`, see
   "Exchange feature flags" above) → **Exchange disabled** — the price
   shown is frozen at its last known value; "today's P/L" is suppressed
   rather than computed from stale bars.
3. Most recent price bar for that stock is synthetic/demo/mirror-fallback → **Demo/Synthetic**
4. `Stock.updated_at` older than `STALE_DATA_DAYS` (4, shared with the ops-alerts threshold) → **Stale**
5. Exchange session open and refreshed within 5 minutes (matches the ticker's own LIVE threshold) → **Live**
6. Exchange session open but older than 5 minutes → **Delayed**
7. Exchange session closed → **Market closed**

### Live price updates

The portfolio page never blocks a request on a live market fetch — prices
come only from `Stock.last_price`/`PriceHistory`, written exclusively by the
existing background sync pipeline (`autosync`, `dse_fetcher`, `cse_fetcher`,
Celery beat). An open portfolio page polls a lightweight authenticated JSON
endpoint (`GET /portfolio/<id>/quotes.json`, rate-limited, cache/DB-only —
same pattern as the public `/ticker.json`) every 20s during DSE/CSE market
hours or 60s outside them, matching `static/js/ticker.js`'s own cadence, and
pauses entirely while the browser tab is hidden.

### Routes

- `GET /portfolio/` — redirect to the caller's default portfolio (auto-created)
- `GET /portfolio/list/`, `POST /portfolio/create/`
- `GET /portfolio/<id>/` — dashboard: summary cards, holdings, allocation, recent transactions
- `POST /portfolio/<id>/rename/`, `POST /portfolio/<id>/set-default/`, `POST /portfolio/<id>/delete/` (requires typing the portfolio name to confirm)
- `GET|POST /portfolio/<id>/holdings/add/` — simplified first-BUY flow
- `GET|POST /portfolio/<id>/transactions/add/`, `GET|POST /portfolio/<id>/transactions/<txn_id>/edit/`, `POST /portfolio/<id>/transactions/<txn_id>/delete/`
- `GET /portfolio/<id>/transactions/` — paginated history with filters
- `GET /portfolio/<id>/quotes.json` — polling endpoint (auth required, rate-limited)

### API (all endpoints require token auth; every lookup is scoped to the caller)

- `GET|POST /api/portfolios/` — list/create
- `GET|PATCH|DELETE /api/portfolios/<id>/`
- `GET /api/portfolios/<id>/summary/` — full summary (totals, best/worst, allocation)
- `GET /api/portfolios/<id>/holdings/`
- `GET|POST /api/portfolios/<id>/transactions/`
- `GET|PUT|PATCH|DELETE /api/portfolios/<id>/transactions/<txn_id>/`
- `GET /api/portfolios/<id>/quote-snapshot/` — lightweight latest-quote poll

All monetary/quantity values are returned as **strings** (e.g.
`"market_value": "42.00"`), never JSON numbers, so no client's JSON parser
can round-trip a Decimal through a float.

### Migrations

`market/migrations/0011_portfolio_portfoliotransaction_and_more.py` adds the
`Portfolio` and `PortfolioTransaction` tables plus their constraints/indexes.
Apply with the usual `python manage.py migrate`.

### Tests

```bash
python manage.py test market.tests.test_portfolio
```

67 tests covering: ownership/cross-user access, auth requirements, default
portfolio behavior + uniqueness, BUY/SELL validation (negative/zero
quantity/price/fees), overselling prevention (including date-aware checks
and edit/delete-time re-validation), WAC math (single/blended buys, partial
and complete sells, fee treatment, realized-vs-unrealized independence),
percentage calculations, zero-cost-basis and missing-price edge cases
without division errors, DSE/CSE stocks sharing a trading code, future-dated
transactions, quote-status labeling for all six states, the web views (POST-
only mutations, CSRF enforcement, no live fetch ever triggered), the DRF API
(auth, cross-user 404s, string-encoded Decimals), and query-count bounds for
a portfolio with many holdings.

### Known limitations

- No performance/value history chart — the app doesn't snapshot daily
  portfolio value over time, so a real historical-performance chart isn't
  possible without fabricating data. The allocation breakdowns (by stock,
  exchange, sector) are real and live.
- P/E-style fundamentals aren't part of this feature (see the ML section for
  the separate, unrelated `pe_ratio` fetcher).
- Figures are personal-tracking estimates from delayed/cached market data —
  explicitly **not** a brokerage statement, tax document, or investment
  advice (shown as a standing disclaimer on every portfolio page/API summary
  response).

## API

- `GET /api/screener/` — potential shares / experimental research candidates / sells (`is_experimental_candidate`, not `is_safe_buy` — see Phase 7)
- `GET /api/stocks/`
- `GET /api/stocks/DSE/GP/`
- `GET /api/stocks/DSE/GP/predict-price/?date=YYYY-MM-DD`
- `GET /api/analysis/`
- `GET /api/backtests/`
- `POST /api/auth/register/` `{username,password,email}`
- `POST /api/auth/login/` `{username,password}` → token
- `GET /api/alerts/` (auth)
- `GET|POST /api/portfolios/`, `GET/PATCH/DELETE /api/portfolios/<id>/` (auth, own records only — see "Portfolio" above)

## Notifications

Set in `.env`:

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

User-level Telegram chat IDs can also be set under **Profile**.

## Feedback & improvement requests

Authenticated Users and Staff can report bugs, data/prediction problems, and
feature requests through **Feedback** (in the nav for both roles, plus a
quick-action on the User/Staff/Admin panels, plus a link on the 403 page).
There is no anonymous submission.

### Routes

| Route | Who | Purpose |
|---|---|---|
| `/feedback/submit/` | User, Staff | Submit a new report |
| `/feedback/mine/` | User, Staff | Their own submissions only |
| `/feedback/<id>/` | Owner, or Staff/Admin (Admin-only categories excluded for Staff) | Detail + timeline |
| `/feedback/<id>/follow-up/`, `/withdraw/`, `/dispute/` | Owner only | Add info / withdraw / "still unresolved" |
| `/feedback/triage/` | Staff, Admin | Searchable/filterable/paginated queue |
| `/feedback/<id>/status/`, `/note/`, `/assign/`, `/assign-to-me/`, `/response/` | Staff, Admin (scope-limited, see below) | Triage actions |
| `/feedback/<id>/priority/`, `/duplicate/` | Admin only | Final priority, duplicate linking |
| `/feedback/admin/dashboard/` | Admin only | Volume/backlog/review-time stats |
| `/feedback/admin/export/` | Admin only | Safe CSV summary (see "Export scope") |

### Roles & permissions

| Capability | User | Staff | Admin |
|---|---|---|---|
| Submit feedback | ✓ | ✓ | ✓ |
| View own submissions | ✓ | ✓ | ✓ |
| View any submission | ✗ | ✓ (excl. Account Issue) | ✓ |
| Add follow-up / withdraw own | ✓ | ✓ | ✓ |
| Dispute a resolved item ("still unresolved") | ✓ (own) | — | — |
| Move to Under Review / In Progress / Duplicate / Cannot Reproduce | ✗ | ✓ | ✓ |
| Move to Planned / Implemented / Resolved / Declined | ✗ | ✗ | ✓ |
| Add internal notes | ✗ | ✓ | ✓ |
| Post the public Admin response | ✗ | ✓ | ✓ |
| Assign to self | ✗ | ✓ | ✓ |
| Assign to anyone | ✗ | ✗ | ✓ |
| Set final (Admin) priority | ✗ | ✗ | ✓ |
| Mark/link duplicates | ✗ | ✗ (may only note a suspected duplicate in an internal note) | ✓ |
| Access "Account Issue" category | ✗ (only own) | ✗ | ✓ |
| Export summary | ✗ | ✗ | ✓ |

Every row is enforced server-side in `feedback/services.py` (the views never
mutate a `Feedback` row directly), on top of the same `reporter=request.user`
ownership scoping and `admin_required`/`staff_or_admin_required` decorators
the rest of the app uses — no second authorization system. The submit form
(`feedback/forms.py`) has no `status`/`admin_priority`/`assigned_to`/
`internal_notes` field at all, so a manipulated POST body containing them
has no effect regardless of what's sent — the fields simply aren't read.

### Model & reference numbers

`feedback.models.Feedback` — reporter (SET_NULL + a `reporter_username_snapshot`
so a record outlives a deleted account, same pattern as
`market.models.AdminAuditLog`), `reference_number` (stable `FB-000123` format,
derived from the row's own primary key on first save), category, title,
description, reporter/admin priority, status, page/feature, steps-to-reproduce,
expected/actual behavior, assignment, public `admin_response` vs. private
`internal_notes` (never serialized to a regular User — enforced at the view/
template layer, not just "not linked from the UI"), `contact_allowed`,
server-captured diagnostic metadata (see below), and `created_at`/`updated_at`/
`reviewed_at`/`resolved_at`. `feedback.models.FeedbackEvent` is a small,
append-only audit trail (status changes, priority sets, assignment, notes,
responses, follow-ups) — one row per change, mirroring `AdminAuditLog`'s
shape. Nothing is ever hard-deleted: withdrawing/declining/resolving all just
change `status`.

### Statuses & workflow

```text
New -> Under Review -> Planned -> In Progress -> Implemented | Resolved
                                        |
                          Duplicate | Cannot Reproduce | Declined | Withdrawn
```

A reporter can withdraw any non-closed item, or mark a `Resolved`/
`Implemented` item "still unresolved" (reopens it to `Under Review`).

### Notifications

Reuses the existing `notifications.models.Alert` system — no second
notification pipeline. `feedback.tasks.notify_reporter` (Celery task,
`notifications` queue) creates an in-app `Alert` for the reporter on:
report received, a status change, and a public response being posted.
Telegram/email are sent additionally only if the reporter's profile has that
channel enabled *and* it's actually configured (`TELEGRAM_BOT_TOKEN` /
`EMAIL_HOST`) — same gating `notifications.tasks.send_daily_digest` already
uses. **Internal-note edits never notify** (the service function that adds
one simply never calls `notify_reporter`).

### Prediction/Data Issue metadata

For those two categories, the reporter names an exchange + trading code (a
plain text hint, not a diagnostic payload) and
`feedback.services.capture_diagnostic_metadata` looks up the *real*, current
`Stock`/`AnalysisResult` rows for it server-side — exchange, trading code,
analysis date, quote timestamp, data source, a prediction reference. Nothing
else the client might submit for those fields is ever read; the form doesn't
declare them. Submitting feedback never writes to `Stock`, `PriceHistory`,
`AnalysisResult`, or `MLModelVersion` — it's strictly read-only against
market/ML data.

### Attachments

**Not implemented.** This deployment has no configured object storage /
persistent media backend beyond the local filesystem already used for ML
model artifacts, and the spec's own guidance is to omit attachments rather
than build unsafe ad-hoc file storage: "if secure persistent storage is
unavailable, omit attachments and document the limitation." A user should
describe/paste relevant detail in the description instead. Revisit if S3-
compatible storage is added to the deployment.

### Export scope

`/feedback/admin/export/` is a CSV with exactly: reference number, category,
reporter priority, admin priority, status, title, created/resolved dates.
Deliberately excludes description, steps-to-reproduce, internal notes,
reporter identity/contact info, and diagnostic metadata — safe to hand to
anyone planning upgrade work without a second review pass.

### Retention

Feedback rows are never hard-deleted by any code path in this app — a
withdrawn/declined/resolved item stays queryable (status-filtered) forever
for historical/upgrade-planning purposes. If retention limits are ever
needed, add them as an explicit, separate management command/policy rather
than folding deletion into the status-change flow.

### Rate limiting & input safety

Submission and follow-up/comment actions are rate-limited per user
(`market.services.rate_limit.is_rate_limited` — the same limiter
`ticker_json`/`portfolio_quotes_json` already use, no second rate-limiting
system). All text fields are rendered through Django's normal auto-escaping
(no `|safe`, no raw HTML) — a `<script>` in a description renders as inert
text, never executes.

### Testing

```bash
DJANGO_SETTINGS_MODULE=config.settings.test python manage.py test feedback
DJANGO_SETTINGS_MODULE=config.settings.test python manage.py test market.tests.test_hybrid_automation
```

## Disclaimer

Bazaar produces **probabilistic** estimates from historical patterns. It is educational software, not licensed investment advice. Bangladesh market conditions (liquidity, manipulation, regulation) can invalidate any model quickly.
