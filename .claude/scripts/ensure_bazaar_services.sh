#!/bin/bash
# Ensures Redis + the Celery worker + Celery beat are running for the
# Bazaar project, idempotently. Safe to run on every session start:
# each service is only (re)started if it isn't already alive, so this
# never spawns duplicate workers/beats across repeated session starts
# (that duplication is exactly what caused a queue backlog and lock
# contention earlier — see docs/RUNBOOKS.md).
set -uo pipefail

PROJECT_DIR="/Users/istiaque/python/Trial"
VENV_BIN="/Users/istiaque/python/Trial/.venv/bin"
STATE_DIR="/tmp/bazaar-services"
# NOTE: $VENV_BIN/celery's own shebang is a stale absolute path (this venv
# appears to have been copied rather than created fresh), so it must be
# invoked as `python3 -m celery`, never as the `celery` script directly —
# the latter silently execs a different, possibly out-of-sync interpreter.
mkdir -p "$STATE_DIR"

cd "$PROJECT_DIR" || exit 0

log() { echo "[ensure_bazaar_services] $*"; }

# --- Redis (Celery's broker) ---
if ! redis-cli ping >/dev/null 2>&1; then
  log "Redis not responding, starting..."
  brew services start redis >/dev/null 2>&1 || redis-server --daemonize yes >/dev/null 2>&1
  for _ in 1 2 3 4 5; do
    redis-cli ping >/dev/null 2>&1 && break
    sleep 1
  done
  if redis-cli ping >/dev/null 2>&1; then
    log "Redis is up."
  else
    log "WARNING: could not confirm Redis is up. Celery worker/beat will fail to start."
  fi
else
  log "Redis already running."
fi

# --- Celery worker ---
# "Running" isn't the same as "healthy": market.services.autosync's own
# try/except catches things like import errors and returns them as
# {"ok": False, "error": ...} instead of raising, so a broken worker can
# sit there looking alive (pidfile present, process up) while every sync
# silently fails — exactly what happened for hours on 2026-08-02. Reuse
# the same detection ops_alerts.py uses for the dashboard/ops-report
# warnings, and restart the worker if its last successful-looking run
# actually recorded an error.
WORKER_PIDFILE="$STATE_DIR/worker.pid"
worker_running=false
if [ -f "$WORKER_PIDFILE" ] && kill -0 "$(cat "$WORKER_PIDFILE" 2>/dev/null)" 2>/dev/null; then
  worker_running=true
fi

if $worker_running; then
  HEALTH=$("$VENV_BIN/python3" manage.py shell -c "
from market.services.ops_alerts import recent_silent_sync_error, FETCH_TASK_NAMES
unhealthy = any(recent_silent_sync_error(name) for name in FETCH_TASK_NAMES)
print('UNHEALTHY' if unhealthy else 'HEALTHY')
" 2>/dev/null | tail -1)
  if [ "$HEALTH" = "UNHEALTHY" ]; then
    log "Celery worker is running but its last sync silently failed — restarting to pick up current code..."
    kill "$(cat "$WORKER_PIDFILE")" 2>/dev/null
    sleep 1
    rm -f "$WORKER_PIDFILE"
    worker_running=false
  else
    log "Celery worker already running (pid $(cat "$WORKER_PIDFILE")) and healthy."
  fi
fi

if ! $worker_running; then
  log "Starting Celery worker..."
  # --pool=solo: default prefork concurrency spawns one child per CPU
  # core, and this project's exclusive_db_write lock only serializes
  # writes *within* code paths that use it — concurrent pool children
  # racing against local dev's single-writer SQLite file (or against ad-
  # hoc `manage.py shell` commands that don't take the lock at all) is
  # exactly what caused a full day of "database is locked" failures on
  # 2026-08-03. Docker/Postgres has no such constraint and keeps its own
  # (higher) worker concurrency separately.
  nohup "$VENV_BIN/python3" -m celery -A config worker -l info --pool=solo \
    --pidfile="$WORKER_PIDFILE" \
    > "$STATE_DIR/worker.log" 2>&1 &
  disown
fi

# --- Celery beat ---
BEAT_PIDFILE="$STATE_DIR/beat.pid"
if [ -f "$BEAT_PIDFILE" ] && kill -0 "$(cat "$BEAT_PIDFILE" 2>/dev/null)" 2>/dev/null; then
  log "Celery beat already running (pid $(cat "$BEAT_PIDFILE"))."
else
  log "Starting Celery beat..."
  nohup "$VENV_BIN/python3" -m celery -A config beat -l info \
    --pidfile="$BEAT_PIDFILE" \
    -s "$STATE_DIR/celerybeat-schedule" \
    > "$STATE_DIR/beat.log" 2>&1 &
  disown
fi

exit 0
