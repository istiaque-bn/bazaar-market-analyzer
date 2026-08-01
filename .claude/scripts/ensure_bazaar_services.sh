#!/bin/bash
# Ensures Redis + the Celery worker + Celery beat are running for the
# Bazaar project, idempotently. Safe to run on every session start:
# each service is only (re)started if it isn't already alive, so this
# never spawns duplicate workers/beats across repeated session starts
# (that duplication is exactly what caused a queue backlog and lock
# contention earlier — see docs/RUNBOOKS.md).
set -uo pipefail

PROJECT_DIR="/Users/istiaque/python/Trial"
VENV_BIN="/Users/istiaque/Desktop/Test/Trial/.venv/bin"
STATE_DIR="/tmp/bazaar-services"
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
WORKER_PIDFILE="$STATE_DIR/worker.pid"
if [ -f "$WORKER_PIDFILE" ] && kill -0 "$(cat "$WORKER_PIDFILE" 2>/dev/null)" 2>/dev/null; then
  log "Celery worker already running (pid $(cat "$WORKER_PIDFILE"))."
else
  log "Starting Celery worker..."
  nohup "$VENV_BIN/celery" -A config worker -l info \
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
  nohup "$VENV_BIN/celery" -A config beat -l info \
    --pidfile="$BEAT_PIDFILE" \
    -s "$STATE_DIR/celerybeat-schedule" \
    > "$STATE_DIR/beat.log" 2>&1 &
  disown
fi

exit 0
