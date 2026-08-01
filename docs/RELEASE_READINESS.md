# Phase 10 — Final independent release-readiness audit

Date: 2026-07-30. Scope: everything built in Phases 1–9. This document is
the audit deliverable — verdicts, exact evidence, remaining risks, and
manual decisions still needed. **No deployment, infrastructure change, or
real-money trading capability was created or enabled by this phase.**
Real-money order execution does not exist anywhere in this codebase
(verified: zero references to order placement/broker execution APIs).

## Verdicts

| Area | Verdict |
|---|---|
| Security | **PASS** |
| Reliability | **PASS WITH CONDITIONS** |
| Data quality | **PASS WITH CONDITIONS** |
| ML validity | **PASS** |
| Backtesting | **PASS WITH CONDITIONS** |
| UX honesty | **PASS** |
| Operations | **PASS** |

None of the seven areas fail outright. Three carry conditions that a human
must weigh before calling this "production ready" in any sense beyond
"the software does what it claims, correctly and honestly." **The most
important finding of this audit is in Backtesting: the trading strategy
does not currently demonstrate outperformance vs. simply holding the
market — see below. This alone is sufficient reason not to claim
profitability.**

---

## 1. Checks (formatting/lint, Django checks, deploy checks, migrations, tests, coverage)

- **No linter or formatter is configured anywhere in this project** — no
  `.flake8`, `pyproject.toml` tool sections, `.pylintrc`, `ruff`/`black`
  config, and none installed in the venv. This was true before this audit
  and remains true; I did not introduce one unprompted. **This is a real
  gap** — see Reliability conditions.
- `python manage.py check` (development settings): `System check
  identified no issues (0 silenced).`
- `python manage.py check --deploy` (production settings, fully populated
  valid env): `System check identified no issues (0 silenced).`
- `python manage.py makemigrations --check --dry-run`: `No changes
  detected` (exit 0) — no model changes without a migration.
- Full test suite, **both** settings modules: `Ran 391 tests ... OK`
  under `config.settings.development` (the default) and again under
  `config.settings.test`.
- Coverage: **69.1%** total (gate `fail_under=45` in `.coveragerc`,
  passes comfortably). Critical modules individually well above that —
  see README "Testing & coverage" for the per-module breakdown.

## 2. Permissions — verified separately, live, for all four tiers

Ran a real request matrix (anonymous / ordinary user / staff / superuser)
against every route class. Exact results:

| Route | Anonymous | Ordinary | Staff | Admin (superuser) |
|---|---|---|---|---|
| Public pages (`/`, `/dashboard/`, `/stocks/`, stock detail, `/backtests/`, `/alerts/`, `/health*`, `/ticker.json`) | 200 | 200 | 200 | 200 |
| `/watchlist/`, `/accounts/profile/` (login-only) | 302→login | 200 | 200 | 200 |
| `/data-quality/`, `/ops/` (staff-only) | 302→login | 302→login | 200 | 200 |
| `POST /pipeline/` (staff-only action) | 302→login (blocked) | 302→login (blocked) | 302→dashboard (**succeeded**) | 302→dashboard (**succeeded**) |
| `GET /admin/` | 302→login | 302→login | 200 (but see below) | 200 |

**Important nuance found and confirmed**: a plain `is_staff=True` user
with no explicit Django permissions can log into `/admin/` but the admin
index shows **zero models** (empty page) and a real attempt to run the
"Deactivate model version" admin action returns **403 Forbidden** with
`is_active` left unchanged. Only a superuser (or a staff user explicitly
granted the relevant model permission) can actually see/act on model
data in Django admin. `@staff_member_required` custom views
(`/pipeline/`, `/data-quality/`, `/ops/`) only check `is_staff`, which is
intentional and correctly scoped (pipeline triggers and read-only ops
reports are lower-stakes than raw model mutation) — but it means "staff"
and "administrator" are genuinely different privilege tiers here, not
synonyms. This is good least-privilege design, working as intended.

API (DRF): `/api/alerts/` requires token/session auth (401/403 for
anonymous, verified by `api/tests/test_auth_pagination.py`); every other
API GET endpoint is intentionally `AllowAny` (read-only, public market
data — consistent with the web pages being public); registration/login
are rate-limited (5/hour, 10/min).

*Process note*: my first pass at this verification used
`DJANGO_SETTINGS_MODULE=config.settings.test` with an ad-hoc script
outside `manage.py test`. That settings module does **not** override
`DATABASES`, so — outside the test runner's own isolated-database setup —
it pointed at the real `db.sqlite3`, and the script's throwaway users and
a fake `MLModelVersion` row were briefly written to the real database. I
caught this via the routine before/after row-count check, deleted exactly
the rows I had added, and re-verified `Stock`/`PriceHistory`/
`AnalysisResult` counts matched the known-good baseline (785 / 635,076 /
2,354) before continuing. Documenting it here rather than omitting it.

## 3. No unbounded request work; no startup-thread DB/network work

- AST-level scan (not just grep) of every module-level statement across
  `accounts`, `api`, `market`, `notifications`, `config` (excluding
  tests/migrations) for calls involving `requests`, sockets, `connect`,
  `Thread`, or `.objects` at import time: **0 found**.
- Every `AppConfig.ready()` reviewed: `market` does nothing (comment in
  the file explicitly says why — no DB queries, no daemon threads);
  `accounts` only registers a `post_save` signal receiver (the receiver's
  DB work runs when a `User` is later saved during a real request, not at
  import time); `api`/`notifications` have no `ready()` at all.
- Exactly one view enqueues heavy work (`run_pipeline_view`), and it does
  so exclusively via Celery `.delay()`/`chain(...).delay()` — never
  synchronously on the request thread (confirmed by reading the view; no
  other view calls `.delay()`/`.apply()`/`.apply_async()`).
- `/ticker.json` is explicitly documented and coded to never force a live
  fetch on request — it serves cached/DB state only.
- One caveat, not a violation of this requirement but worth recording:
  `/ops/` and `/data-quality/` (both staff-only) do real, bounded
  synchronous DB aggregation over the full 635k-row `PriceHistory` table
  and took roughly 2–7 seconds in this environment. That's bounded
  (fixed-size dataset, no loop that grows per request) but non-trivial —
  worth watching if the staff user base or table size grows.

## 4. Secrets, DEBUG, CORS/CSRF, SSL, dependencies, unsafe serialization

- No hardcoded secret-shaped literals found in application code (pattern
  scan across all non-test `.py` files).
- `.env`, `db.sqlite3`, and any `.pem`/`.key` files: **not tracked by
  git** (`git ls-files` confirms).
- `DEBUG`: hardcoded `False` in `production.py` (env-uncontrollable,
  matches Phase 8 design), env-controlled default `True` in
  `development.py` only.
- CORS: `CORS_ALLOW_ALL_ORIGINS = False` in `base.py`/`production.py`
  (explicit allow-list only in prod); `True` only in `development.py`
  with a documented reason.
- CSRF: `CsrfViewMiddleware` active in all environments;
  `CSRF_TRUSTED_ORIGINS` required (fail-fast) and scheme-validated in
  production; live test confirms an unauthenticated POST without a CSRF
  token gets **403**, not silently accepted.
- SSL: `DSE_SSL_VERIFY` defaults to `True` everywhere (verified via
  certifi's CA bundle); the `verify=False` escape hatch is opt-in only,
  not present in `.env`/`.env.example`/`.env.production.example`, so
  nothing ships with it off.
- `pip-audit` (full environment, fresh run today): **No known
  vulnerabilities found.**
- Unsafe serialization: zero `eval(`/`exec(`/unsafe `yaml.load(` calls.
  Zero `|safe` template filters remain anywhere in `templates/` (Phase 7
  replaced the one prior instance with Django's `json_script` filter —
  confirmed absent). `joblib.dump`/`joblib.load` (pickle-based) is used
  for ML model artifacts — **this is pickle-based deserialization**, a
  real class of risk in general, but the files loaded are exclusively
  ones this app's own training pipeline writes to `data/cache/`, never
  user-supplied or externally fetched. **Manual decision / operational
  requirement**: `data/cache/` must never become writable by an untrusted
  party (e.g. a compromised dependency, a shared/misconfigured volume in
  a container deployment) — if it did, that write access alone would
  already be a worse compromise than the deserialization risk it enables.
  This is a standard, accepted trade-off for Python ML persistence, not a
  code defect, but it belongs in the threat model explicitly.

## 5. Leakage tests and walk-forward evaluation, clean artifact location

- `market/tests/test_ml_training.py`: **26/26 pass** — covers the
  embargo gap (no train row can reach into a test window), imputer
  fit-on-train-fold-only, forward-return labels correctly left `NaN`
  (not `0`) for rows whose 10-day-forward outcome isn't known yet,
  chronological validation, and three end-to-end
  `train_model()`/`train_next_close_model()` runs with `MODEL_PATH`
  monkey-patched to a `tempfile.TemporaryDirectory()` so the real
  `data/cache/*.pkl` files are never touched.
- Additionally ran `build_training_panel()` + `walk_forward_evaluate()`
  **live, read-only, against the real price database** (these functions
  persist nothing — no DB write, no file write) to get real numbers on
  real data, not just synthetic fixtures:

  | Exchange | Panel rows | Folds | Skill vs. majority-class baseline | Direction accuracy |
  |---|---|---|---|---|
  | DSE | 35,234 | 5 | **+0.0153** | 50.8% |
  | CSE | 81,461 | 5 | **+0.0323** | 62.2% |

  Every fold's `train_end` sits a visible embargo gap before its
  `test_start` (e.g. DSE fold 1: train ends 2025-05-13, test starts
  2025-05-27 — the configured 14-day embargo, live on real data, not just
  asserted in a unit test).

## 6. Realistic backtests with costs and benchmarks, untouched period

Cost model (`CostConfig`, documented as "illustrative approximations for
a BD retail brokerage account"): brokerage 0.30%, tax 0.05%, spread
0.10%, slippage 0.10% per side, position sizing capped at 5% of a
stock's recent volume with a 100-share floor — not a zero-cost fantasy
backtest.

Ran a **fresh** trailing-1-year backtest (2025-07-30 → 2026-07-29, a
period not covered by the pre-existing "Phase5 sample 2y" runs already in
the database) for both exchanges:

| Exchange | Trades | Win rate | Strategy return | Annualized | Sharpe | Max DD | Total costs | Buy-hold benchmark | Index benchmark |
|---|---|---|---|---|---|---|---|---|---|
| DSE | 308 | 52.6% | **−6.0%** | −6.0% | −0.31 | −25.0% | 136,168 (of 1,000,000 initial cash) | +24.6% | +26.9% |
| CSE | 247 | 61.9% | **−0.1%** | −0.1% | −0.04 | −2.2% | 19,380 | +22.3% | +32.1% |

**On both exchanges, over the most recent real 1-year period, the
strategy lost money in absolute terms and underperformed both the
buy-and-hold and index benchmarks by a wide margin.** This is the single
most important number in this whole audit. `data_start_date`/
`data_end_date` never exceeded the requested window (2026-07-29, not
2026-07-30, because today's session hadn't been appended yet at run
time) — the "never wider than requested" invariant holds. `include_demo`
defaults `False` — no synthetic data in these results. Realistic
liquidity-based position-size warnings fired throughout (e.g. "sized down
... on liquidity/cash limit"), meaning the backtest isn't assuming
infinite fills either.

The earlier (pre-existing) 2-year "Phase5 sample" runs are more mixed
(CSE modestly positive but still behind its index; DSE substantially
negative), so this isn't a one-off bad window — it's consistent with the
strategy not having a demonstrated edge net of realistic costs over
either period examined.

## 7. Demo/live separation and data-provenance coverage

- `provenance_report()` (live, real DB): 635,076 total `PriceHistory`
  rows, **0 currently synthetic**, 635,076 (100%) still carry `unknown`
  provenance (every row predates Phase 6's tracking; Phase 6 documented
  this is not reconstructible after the fact, so it's honestly labeled
  rather than guessed as live), 154,126 (24.3%) carry at least one
  quality flag.
- `.live()` queryset exclusion, demo-isolation defaults
  (`include_demo=False` on `analyze_stock`, `run_full_analysis`,
  `run_backtest`, `PortfolioBacktester`, `build_training_panel`), and
  synthetic-can't-clobber-real write protection are covered by the Phase
  6 test suite (part of the 391 passing).

## 8. Weak/stale/negative-skill models are not shown as actionable or safe

Live-rendered a real stock detail page (`/stocks/DSE/ARGONDENIM/`) and
confirmed by inspecting the actual HTML text (not just reading the code):

- The next-day close forecast panel — whose real current live skill is
  **−0.0438 over 629 settled forecasts** (worse than the naive "tomorrow
  = today" baseline; 49% direction accuracy, worse than a coin flip) —
  renders **"No demonstrated predictive edge"** in place of confident
  forecast language, exactly as designed.
- The word "safe"/"Safe"/"SAFE" appears on that page exactly twice, both
  as explicit disclaimers ("nothing here is a safety... certification",
  "not guarantees of future price, safety, or performance") — never as
  an unqualified claim.
- Dashboard and `/api/screener/` both use `is_experimental_candidate`
  language, not `is_safe_buy` (confirmed absent from both the rendered
  HTML and the raw API JSON).
- Separately, the forward-return classifier **is** currently deployed
  with a real, positive walk-forward skill (see §5) — so the overall
  `signal_status.has_edge` is correctly `True` even while the next-close
  panel independently and correctly shows no edge for itself. The two
  layers are evaluated and disclosed independently, not blended into one
  falsely-reassuring or falsely-alarming headline number.

## 9. Backup/rollback documentation and safe isolated restore only

- `docs/RUNBOOKS.md` explicitly states "Never restore directly over the
  live database" and requires `verify_backup` to print `RESTORE VERIFIED`
  first — no auto-restore path exists anywhere in the codebase.
- Took a **fresh** real backup this session (`backups/20260730_030738/`)
  and verified it in isolation:
  ```
  [OK] sha256:db.sqlite3
  [OK] sha256:forward_return_rf.pkl
  [OK] sha256:next_close_rf.pkl
  [OK] sha256:autosync_state.txt
  [OK] manifest_parses
  [OK] sqlite_integrity_check — ok
  [OK] row_count:Stock — restored=785 manifest=785
  [OK] row_count:PriceHistory — restored=635076 manifest=635076
  [OK] row_count:AnalysisResult — restored=2354 manifest=2354
  RESTORE VERIFIED
  ```
- No restore was performed against the live database or any
  production-shaped target — only the isolated scratch-directory check
  above, per the instruction to perform *only* safe isolated restore
  checks.
- `--prune-keep 5` correctly left all 3 existing backup directories in
  place (below the keep threshold — nothing was deleted).
- Known, previously-documented limitation (unchanged from Phase 9): the
  PostgreSQL backup path can only be structurally checked
  (`pg_restore --list`) in this environment — no live Postgres instance
  exists here to prove a real row-count-verified restore. **Manual
  decision required**: before ever relying on Postgres backups in a real
  deployment, run one real restore drill against a disposable Postgres
  instance.

---

## Remaining risks (not fixed by this audit — flagged for a human decision)

1. **No demonstrated trading edge net of costs, most recent 1-year
   window, either exchange** (§6). This is the headline risk. Any
   external communication about this project must not claim
   profitability, "safe," or "production-ready for real trading" — none
   of that is supported by the evidence gathered today, and some of it is
   directly contradicted.
2. **100% of price history predates provenance tracking** and can never
   be retroactively verified as genuinely live (§7) — this is permanent,
   not a to-do item; it must stay disclosed indefinitely, not just until
   someone "gets around to" fixing it.
3. **24.3% of price rows are flagged** (mostly `open_out_of_range`,
   ~137k rows) — flagged, not deleted, per the Phase 6 design, but the
   root cause in the DSE/CSE fetch/parsing path has never been
   investigated. Recommended, not done here (would be new feature work
   in a phase explicitly scoped to "no major new features").
4. **No linter/formatter/type-checker and no CI pipeline configured**
   anywhere in this repo. The test suite is genuinely strong (391 tests,
   69% coverage, passes under two settings modules) and substitutes for
   a lot of what linting would catch, but nothing currently stops a
   future change from silently regressing style/typing/security patterns
   before a human reviews it.
5. **PostgreSQL backup/restore is only structurally verified**, not
   proven via a real restore (no Postgres instance available here) — see
   §9.
6. **`DJANGO_SETTINGS_MODULE` defaults to `development`** if a deploy
   forgets to set it explicitly to `production` — documented as a
   deploy-checklist item in `docs/DEPLOYMENT.md` rather than something
   the code can fully self-detect (see that doc for the reasoning).
7. **Ops/data-quality staff pages take several seconds** against the
   current 635k-row table — bounded, not a live risk today, but worth
   watching if data volume or staff traffic grows materially.

## Manual decisions needed before any real-world use

- Whether to invest in the root-cause data-quality investigation (risk 3)
  before trusting flagged rows for anything beyond research.
- Whether/when to adopt a linter + CI pipeline (risk 4).
- Whether to run a real Postgres backup/restore drill before a Postgres
  deployment is trusted (risk 5).
- Above all: **whether this project is used for anything beyond
  research/education**, given risk 1. The code and this audit do not
  support using it to manage real money, and no order-execution
  capability exists to even attempt that today.
