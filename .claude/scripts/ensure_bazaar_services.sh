#!/bin/bash
# Ensures Redis + the Celery worker + Celery beat are running for the
# Bazaar project, idempotently. Safe to run on every session start:
# each service is only (re)started if it isn't already alive, so this
# never spawns duplicate workers/beats across repeated session starts
# (that duplication is exactly what caused a queue backlog and lock
# contention earlier — see docs/RUNBOOKS.md).
#
# 2026-08-19 incident: the pidfile check below is a plain check-then-act
# (TOCTOU) with no lock, so two invocations racing within the same second
# (e.g. two Claude Code sessions starting close together) could both see
# "not running" and both start a worker/beat — only the second one's PID
# survives in the pidfile, orphaning the first. Nothing ever swept
# processes the pidfile *doesn't* currently track, so five orphaned
# worker/beat processes from Aug 2-12 silently accumulated over two
# weeks, all still holding SQLite open, none visible to this script's
# single-PID check. By the time this was found, even the "tracked" pair
# had gone 2 days without a restart and drifted onto stale code (a
# worker that doesn't restart never picks up new task registrations).
# Fixed with: (1) an mkdir-based mutex around the whole start sequence
# so two concurrent invocations can't race, (2) a sweep that kills any
# worker/beat process for this project that ISN'T the one the pidfile
# tracks, every run, regardless of how it got orphaned.
set -uo pipefail

PROJECT_DIR="/Users/istiaque/python/Trial"
VENV_BIN="/Users/istiaque/python/Trial/.venv/bin"
STATE_DIR="/tmp/bazaar-services"
LOCKDIR="$STATE_DIR/ensure.lock"
# NOTE: $VENV_BIN/celery's own shebang is a stale absolute path (this venv
# appears to have been copied rather than created fresh), so it must be
# invoked as `python3 -m celery`, never as the `celery` script directly —
# the latter silently execs a different, possibly out-of-sync interpreter.
mkdir -p "$STATE_DIR"

cd "$PROJECT_DIR" || exit 0

log() { echo "[ensure_bazaar_services] $*"; }

# Serialize the whole check-and-maybe-start sequence across concurrent
# invocations (e.g. two session starts within the same second) so they
# can't both decide "nothing running" and both spawn a worker/beat.
# `mkdir` is atomic on every filesystem this runs on and macOS ships no
# `flock` binary by default, so this is the portable option — a second
# concurrent invocation spins until the first one rmdir's the lock (or
# until it gives up after 30s and proceeds anyway rather than hanging
# the session start forever; a stale lock from a crashed prior run is
# removed after 60s so this is self-healing too).
lock_acquired=false
for _ in $(seq 1 30); do
  if mkdir "$LOCKDIR" 2>/dev/null; then
    lock_acquired=true
    break
  fi
  if [ -n "$(find "$LOCKDIR" -maxdepth 0 -mmin +1 2>/dev/null)" ]; then
    log "Stale lock (>60s old) at $LOCKDIR, removing."
    rmdir "$LOCKDIR" 2>/dev/null
    continue
  fi
  sleep 1
done
# Only release a lock this invocation actually acquired — rmdir'ing an
# unacquired lock after the 30s give-up-and-proceed-anyway path would
# tear down whichever other invocation is genuinely holding it.
if $lock_acquired; then
  trap 'rmdir "$LOCKDIR" 2>/dev/null' EXIT
else
  log "Could not acquire lock after 30s, proceeding without it."
fi

# Sweep: kill any worker/beat process for this project that the pidfiles
# don't currently point at — orphans from a past race, a crash before
# the pidfile was written, or a stale PID that was never cleaned up.
# Keeps at most one of each alive; the block below then (re)starts
# whichever one is now missing.
sweep_orphans() {
  local pattern="$1" pidfile="$2"
  local tracked=""
  [ -f "$pidfile" ] && tracked="$(cat "$pidfile" 2>/dev/null || true)"
  pgrep -f "$pattern" 2>/dev/null | while read -r pid; do
    if [ "$pid" != "$tracked" ]; then
      log "Killing orphaned process (pid $pid, not tracked by $pidfile): $pattern"
      kill "$pid" 2>/dev/null
    fi
  done
}
sweep_orphans "$VENV_BIN/python3 -m celery -A config worker" "$STATE_DIR/worker.pid"
sweep_orphans "$VENV_BIN/python3 -m celery -A config beat" "$STATE_DIR/beat.pid"

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
