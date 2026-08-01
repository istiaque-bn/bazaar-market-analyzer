#!/bin/bash
# Pull the latest code from GitHub and redeploy the Docker stack.
# Run from anywhere: bash /Users/istiaque/python/Trial/scripts/update.sh
set -euo pipefail

PROJECT_DIR="/Users/istiaque/python/Trial"
cd "$PROJECT_DIR"

echo "== git pull =="
git pull origin main

echo "== rebuilding images =="
docker compose --env-file .env.docker build

echo "== recreating containers (migrate/collectstatic run automatically on web's startup) =="
docker compose --env-file .env.docker up -d

echo "== done. Recent web logs: =="
docker compose --env-file .env.docker logs --tail=30 web
