#!/bin/bash
# Local dev runner: brings up Celery (worker + embedded beat) alongside
# `manage.py runserver` so the local SQLite DB keeps getting live DSE/CSE
# data and scheduled jobs, the same way the Docker stack does for
# production. Uses the local Redis instance at CELERY_BROKER_URL in .env
# (redis://localhost:6379/3 — a different DB index than Docker's, so the
# two never collide even if both happen to be running).
#
# Ctrl+C stops runserver AND kills the background Celery process.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

PYTHON=".venv/bin/python3"

if ! redis-cli ping >/dev/null 2>&1; then
    echo "Redis isn't running on localhost:6379 — Celery needs it. Start it with 'brew services start redis' or 'redis-server &' and re-run." >&2
    exit 1
fi

echo "Starting Celery worker + beat (embedded) in the background..."
# Invoke as `python3 -m celery`, not the `celery` console script — this
# venv's celery script has a stale shebang pointing at a different,
# possibly out-of-sync interpreter (see ensure_bazaar_services.sh).
"$PYTHON" -m celery -A config worker -B -l info --pool=solo &
CELERY_PID=$!

cleanup() {
    echo "Stopping Celery (pid $CELERY_PID)..."
    kill "$CELERY_PID" 2>/dev/null
    wait "$CELERY_PID" 2>/dev/null
}
trap cleanup EXIT INT TERM

echo "Starting Django dev server..."
"$PYTHON" manage.py runserver
