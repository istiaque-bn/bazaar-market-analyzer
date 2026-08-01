# Incident response & rollback runbooks (Phase 9)

Companion to [`docs/DEPLOYMENT.md`](DEPLOYMENT.md) (settings/infra) and the
[Data provenance & quality](../README.md#data-provenance--quality-phase-6)
and [Model training & evaluation](../README.md#model-training--evaluation-phase-4)
README sections. This document covers *what to do when something is
already wrong* — detection, mitigation, and rollback — not initial setup.

No step here connects to a paid or external monitoring/alerting service.
Detection is: the structured logs (`config/logging_utils.py`), the
`/health/live/` and `/health/ready/` endpoints, the staff-only
[`/ops/`](../market/views.py) report page, and
`python manage.py ops_alerts_scan`. Wiring any of this into a paid
service (PagerDuty, Sentry, Datadog, etc.) is a deliberate future
decision, not done here — see "Remaining decisions" at the end.

## How to find out something is wrong

| Signal | Where |
|---|---|
| Process won't route requests at all | `/health/live/` returns non-200 or times out |
| A dependency (DB, broker) is down | `/health/ready/` returns 503 with `checks: {...}` |
| Stale data, repeated fetch failures, stuck jobs, DB errors, model degradation | `/ops/` (staff) or `python manage.py ops_alerts_scan` |
| Rejected/flagged price rows, import batch errors | `/data-quality/` (staff) |
| Exception detail (redacted) for any of the above | application logs — grep by `request_id`/`task_id` from the relevant response header or `TaskRun.id` |
| Who triggered a pipeline run or flipped a model's active status | Django admin → Admin audit logs (`AdminAuditLog`, read-only) |

Every log line carries a `request_id` (web) or `task_id`/`task_name`
(Celery) — see `config/logging_utils.py` and `market/middleware.py`. Use
that to pull every log line for one failing request/task instead of
scrolling raw output. Secrets are redacted before a line ever leaves the
process (pattern-based + literal-value substitution — see that module);
if you ever see what looks like a real secret in a log, that's a bug in
the redaction rules, not something to route around — fix the pattern in
`config/logging_utils.py._PATTERN_REDACTIONS` first.

## Incident: stale data

**Symptom**: `/ops/` shows a `stale_data_<EXCHANGE>` alert; `/data-quality/`
freshness table shows an old `latest_price_date`.

1. Check `/health/ready/` — if the database itself is unreachable, this
   is actually a database incident (see below); fix that first.
2. Check the Celery beat schedule is actually running:
   `celery -A config beat` and `celery -A config worker` processes are
   both up (see README "Background execution"). A beat process that
   died silently produces exactly this symptom with no error anywhere
   else.
3. Check `/ops/` task health for `market.tasks.sync_live_market` /
   `append_daily_bars` — repeated failures point to the fetch upstreams
   (DSE/CSE) being unreachable or having changed shape; see
   `market/services/dse_fetcher.py` / `cse_fetcher.py`'s fallback
   behavior (Phase 6) — a fallback firing is visible as
   `SYNTHETIC_FALLBACK`/`CSE_MIRROR_FALLBACK` rows in `/data-quality/`,
   not as a hard failure, so check there too even if no task is failing.
4. Manual recovery: staff → Dashboard → "Fetch live + analyze", or
   `python manage.py run_market_pipeline --fetch --analyze` from the CLI
   (does not require a worker — runs synchronously).
5. If upstream is down for an extended period, this is expected and not
   a code bug — document the outage window; don't "fix" it by loosening
   the staleness threshold (`market/services/ops_alerts.py:STALE_DATA_DAYS`)
   just to silence the alert.

## Incident: repeated fetch failure

**Symptom**: `repeated_failure_<task>` alert — the last 3 runs of a fetch
task all failed.

1. Read the actual error: `/ops/`'s task health table shows the latest
   error (truncated); the Django admin's `TaskRun` list has the full
   `error` field per run.
2. Distinguish transient vs. structural:
   - Transient (network timeout, DB lock, upstream 5xx) — the task's own
     `autoretry_for`/backoff (see `market/tasks.py`) already handles
     single blips; three in a row means it's not self-healing.
   - Structural (upstream changed response shape/auth, SSL cert issue,
     credentials expired) — needs a code or config fix, not a retry.
3. If it's the DSE/CSE upstream itself, check whether the synthetic
   fallback path is firing (Phase 6 — `SYNTHETIC_FALLBACK` sources) so
   you know whether users are currently seeing stale-but-real or
   clearly-marked-synthetic data in the meantime.
4. After a fix, confirm recovery via `/ops/` (failure streak clears once
   a run succeeds) rather than assuming the fix worked.

## Incident: job overlap / stuck job

**Symptom**: `job_overlap_<task>` (more than one `TaskRun` for the same
task currently `started`) or `stuck_job_<task>` (one has been `started`
for >20 minutes — see `market/services/ops_alerts.py:STUCK_JOB_MINUTES`).

1. Market-writing tasks hold `market.services.autosync.exclusive_db_write`
   (a cross-process Redis lock) — genuine concurrent execution of the
   *actual work* shouldn't happen. A `job_overlap` alert more often means
   a worker crashed after creating its `TaskRun` row (via
   `@record_task_run`) but before the row could be marked
   finished/failed — the row is an orphan, not evidence of a real race.
2. Check whether the worker process is still alive:
   `celery -A config inspect active` (run where a worker can reach the
   broker) shows genuinely in-flight tasks. If the stuck `TaskRun` isn't
   listed there, the worker that started it is gone.
3. If it's an orphaned row: no data-corruption risk (the lock protected
   the actual writes) — it's just a stale status row. Mark it
   `TaskStatus.FAILURE` by hand via Django admin (`TaskRun` isn't
   read-only) with a note, so it stops appearing in "in-flight" checks
   and future `job_overlap`/`stuck_job` scans.
4. If tasks are genuinely piling up (worker alive, just falling behind):
   check `CELERY_WORKER_PREFETCH_MULTIPLIER`/
   `CELERY_WORKER_MAX_TASKS_PER_CHILD` (see `docs/DEPLOYMENT.md`) and
   whether the beat schedule is firing the same task faster than a run
   can complete.

## Incident: database problems

**Symptom**: `/health/ready/` → `checks.database: false`; `/ops/` shows
`database_unreachable`.

1. This is deliberately reported with zero detail in the HTTP response
   (requirement: readiness must not expose sensitive details) — the
   actual exception is in the structured log under
   `market.services.health` at ERROR level, redacted.
2. SQLite (dev/most deployments so far): check disk space (`df -h`) and
   file permissions on `db.sqlite3`; a `database is locked` error
   usually means a long-running write is holding the lock longer than
   `DATABASES.default.OPTIONS.timeout` (60s, see `config/settings/base.py`)
   — check `/ops/` task health for anything abnormally slow.
3. PostgreSQL (production): check the connection details in
   `.env.production.example`'s `POSTGRES_*` vars are still correct
   (rotated password, host change, etc.) and that the database server
   itself is reachable from the app host (network/security-group issue
   vs. Postgres-itself-down — different fixes).
4. If data may be corrupted (not just unreachable): stop here and go to
   **Rollback: database**, below, rather than attempting repairs against
   a database you don't yet trust.

## Incident: model degradation

**Symptom**: `model_degraded_forward_return_<EX>` or
`model_degraded_next_close` on `/ops/` — a deployed model's recorded/live
skill vs. its naive baseline is not positive.

1. This is *expected occasionally* — markets change, and both models are
   already designed to self-protect:
   - The forward-return classifier is only ever deployed
     (`MLModelVersion.is_active`) if its walk-forward skill was positive
     *at training time* (`market/services/ml_model.py`); this alert
     means it has since drifted, not that a bug let a bad model through.
   - The next-close learner self-downgrades automatically
     (`market/services/close_learn.py:_maybe_downgrade_on_live_skill`)
     once live skill goes non-positive over enough settled forecasts —
     this alert may fire in the (short) window before that scheduled
     downgrade runs.
2. Immediate mitigation if it needs to happen *now*, not at the next
   scheduled retrain: Django admin → `MLModelVersion` → select the
   row(s) → action "Deactivate selected model version(s) (audited)".
   This is staff-triggered and fully audited (`AdminAuditLog`,
   `model_deactivated`) — see `market/admin.py`. The web UI's
   `signal_status`/"no demonstrated predictive edge" language (Phase 7)
   picks this up automatically on the next page render, no other change
   needed.
3. Root-cause / longer-term: retrain
   (`python manage.py train_ml_models` or wait for the scheduled Celery
   task) and inspect whether the drift is a real regime shift or a data
   problem — check `/data-quality/` for a recent spike in flagged/
   synthetic rows feeding training, which would explain a bad retrain
   without the model logic itself being at fault.
4. **Rollback a bad retrain**: `MLModelVersion.backup_path` on the
   previous good version's row points at the `.pkl` file
   `ml_training.backup_existing_model()` copied aside before the retrain
   overwrote it (`data/cache/backups/`). To roll back: copy that backup
   file back over the live model path
   (`MLModelVersion.file_path` on the *new*, bad version tells you what
   to overwrite), then use the admin "Reactivate" action on the old
   version's row and "Deactivate" on the bad new one — both audited.

## Rollback: application code

See `docs/DEPLOYMENT.md` — deploy the previous release the same way any
deploy happens (this repo doesn't prescribe a specific CD tool). Nothing
Phase-9-specific here beyond: check `/health/ready/` and `/ops/`
immediately after rollback completes, don't just assume it worked.

## Rollback: database

See `docs/DEPLOYMENT.md`'s SQLite→PostgreSQL migration section for the
production case. For "restore from a Phase 9 backup" specifically:

1. **Never restore directly over the live database.** Always verify in
   isolation first:
   ```bash
   python manage.py verify_backup backups/<timestamp>
   ```
   This must print `RESTORE VERIFIED` — checks SHA256 integrity, restores
   the sqlite file into a throwaway scratch directory (never the real
   `db.sqlite3`), runs `PRAGMA integrity_check` against *that copy*, and
   compares row counts against the manifest recorded at backup time. If
   it prints `RESTORE VERIFICATION FAILED`, that backup is not safe to
   use — try an older one.
2. Only after verification passes: stop the app (web + worker + beat),
   back up the *current* (possibly-bad) `db.sqlite3` first — even a
   backup you're about to discard might have forensic value —
   then copy the verified backup's `db.sqlite3` over the live one.
3. Restart the app, confirm `/health/ready/` and `/ops/` look sane, spot
   check a few known rows.
4. If any ML model `.pkl` files were also restored, confirm their
   corresponding `MLModelVersion.version`/`file_path` rows are
   consistent with what's actually on disk — a restored model file
   without a matching DB row (or vice versa) will make
   `ml_model.load_model()` fail closed (refuse to serve it), not crash,
   per its own fail-closed design, but it's still worth confirming
   deliberately rather than discovering it later.

## Backup schedule & retention

```bash
python manage.py backup_bazaar --prune-keep 14
```

Writes a timestamped directory under `backups/` (db + ML model
artifacts + SHA256SUMS.txt + manifest.json), then deletes backup
directories beyond the most recent 14 **that were themselves produced by
this command** (identified by having both `manifest.json` and
`SHA256SUMS.txt` — an unrelated directory a human left in `backups/` is
never touched). Pruning is opt-in (`--prune-keep`); without it, nothing
is ever deleted.

This is not currently scheduled automatically (no Celery beat entry) —
running it is a deliberate operational decision left to whoever operates
a given deployment, matching Phase 8/9's stance of not silently taking
actions with real infrastructure consequences. Recommended cadence:
daily via cron/systemd-timer outside the app process, e.g.:

```cron
0 3 * * * cd /path/to/bazaar && /path/to/venv/bin/python manage.py backup_bazaar --prune-keep 14 && /path/to/venv/bin/python manage.py verify_backup "$(ls -td backups/*/ | head -1)"
```

**Never claim a backup works until `verify_backup` has actually been run
against it** — a backup that was merely *written* and never verified is
an unverified claim, not a working backup. This is why the cron example
above chains `verify_backup` onto every `backup_bazaar` run rather than
treating "the copy command didn't error" as sufficient.

## Remaining decisions (not made here)

- **External alert delivery**: `ops_alerts_scan --fail-on-critical` is
  designed to be easy to wire into an existing cron/CI/paid monitoring
  tool's "run this, alert on non-zero exit" pattern, but nothing here
  does that wiring — explicit authorization needed before connecting
  any paid or third-party service, per this phase's instructions.
- **Backup off-siting**: `backups/` is local disk only. Copying backups
  off the app host (S3, another machine, etc.) is not done here.
- **Postgres backup verification**: `verify_backup` can only structurally
  check a `pg_dump` file (`pg_restore --list`) without a live Postgres
  instance to restore into — full row-count verification for the
  Postgres path needs a real (ideally disposable) target database,
  which isn't provisioned by this repo. The SQLite path's verification
  is the one actually exercised end-to-end in this environment.
