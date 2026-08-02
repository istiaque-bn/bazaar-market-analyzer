#!/bin/bash
# Run at login via ~/Library/LaunchAgents/com.bazaar.docker-autostart.plist.
# Waits for Docker Desktop's daemon to actually be ready (it takes a
# while to boot its VM after being launched), then brings the whole
# compose stack up. Safe to run repeatedly — `docker compose up -d` is
# idempotent and no-ops on already-running, unchanged containers.
set -uo pipefail

# launchd's default PATH is just /usr/bin:/bin:/usr/sbin:/sbin — it doesn't
# include /usr/local/bin, where Docker Desktop's `docker` CLI symlink lives.
export PATH="/usr/local/bin:$PATH"

PROJECT_DIR="/Users/istiaque/python/Trial"
LOG_DIR="$HOME/Library/Logs/bazaar-docker"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/autostart.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"; }

log "autostart triggered"

# Make sure Docker Desktop itself is launching (its GUI app owns the daemon on macOS).
open -ga Docker 2>>"$LOG_FILE" || true

# Wait up to 3 minutes for the daemon socket to respond.
for i in $(seq 1 90); do
    if docker info >/dev/null 2>&1; then
        log "docker daemon ready after ${i}0s-ish"
        break
    fi
    sleep 2
done

if ! docker info >/dev/null 2>&1; then
    log "ERROR: docker daemon never became ready — giving up"
    exit 1
fi

cd "$PROJECT_DIR" || { log "ERROR: cannot cd to $PROJECT_DIR"; exit 1; }

log "bringing up compose stack..."
docker compose --env-file .env.docker up -d >>"$LOG_FILE" 2>&1
log "compose up exit code: $?"
