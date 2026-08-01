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
python manage.py createsuperuser  # optional
python manage.py run_market_pipeline --all
python manage.py runserver
```

Open http://127.0.0.1:8000/

Settings live in `config/settings/` (`base.py` + `development.py` /
`test.py` / `production.py`). `manage.py`/`wsgi.py`/`asgi.py`/`celery.py`
all default to `config.settings.development` — the commands above are
unchanged from before the split. For a production deployment (PostgreSQL,
security headers, WhiteNoise static files, etc.), see
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — that settings module is never
used unless `DJANGO_SETTINGS_MODULE=config.settings.production` is set
explicitly.

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

## Background execution (Celery + django-celery-beat)

**Redis is required.** All background work — live sync, daily append, analysis,
model training, settlement, notifications — runs as named, scheduled Celery
tasks (`market/tasks.py`, `notifications/tasks.py`); nothing starts a thread
at process startup anymore. Web requests (the dashboard's Fetch/Re-analyze
buttons, staff-only) *enqueue* a task and return immediately — they never run
the job on the request thread.

Run all three processes for full local operation:

```bash
# 1. Web server
python manage.py runserver

# 2. Worker — actually executes tasks
celery -A config worker -l info

# 3. Beat — fires the schedule below
celery -A config beat -l info
```

Without a running worker, `/pipeline/` still enqueues successfully (the job
sits queued in Redis) but nothing executes it until a worker starts. The
`run_market_pipeline` management command is unaffected — it calls the
service functions directly, not through Celery, so it still runs
synchronously from the CLI without a worker.

### Beat schedule

| Task | Schedule |
|---|---|
| `market.tasks.sync_live_market` | every 60s (self-limits to `AUTO_SYNC_INTERVAL_MARKET`/`_OFF`) |
| `market.tasks.append_daily_bars` | 10:05 & 14:05, Sun–Thu |
| `market.tasks.close_learn_settlement` | 14:45, Sun–Thu |
| `market.tasks.train_ml_model` | 14:50, Sun–Thu |
| `notifications.tasks.send_daily_digest` | 15:00, Sun–Thu |

### Eager mode — tests only

`CELERY_TASK_ALWAYS_EAGER=True` (env var) makes `.delay()` run a task's body
immediately in-process instead of enqueueing it. This is only meant for
tests that want full inline execution instead of mocking `.delay` — **do not**
set it for the running web server or worker, since that reintroduces
request-thread blocking (the exact problem enqueueing was added to avoid).

## Testing & coverage

```bash
# Run the full suite (Redis must be running — some locking tests use it directly)
python manage.py test accounts api market notifications

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
`ml_training.py` (86.1%). Remaining low-coverage areas (`close_learn.py`'s
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
new runs are `"v2"` and always **insert** a new row rather than overwrite
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
  timestamped `backups/<ts>/` (db + ML model artifacts +
  `SHA256SUMS.txt` + `manifest.json` with a row-count snapshot).
  `python manage.py verify_backup backups/<ts>` performs an **isolated**
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

## Notifications

Set in `.env`:

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

User-level Telegram chat IDs can also be set under **Profile**.

## Disclaimer

Bazaar produces **probabilistic** estimates from historical patterns. It is educational software, not licensed investment advice. Bangladesh market conditions (liquidity, manipulation, regulation) can invalidate any model quickly.
