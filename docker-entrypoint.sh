#!/bin/bash
# Shared entrypoint for all three containers (web/worker/beat) built from
# the same image. SERVICE_ROLE (set per-service in docker-compose.yml)
# decides whether this container also runs migrate/collectstatic —
# only the "web" container does, so three containers starting at once
# don't race each other applying migrations.
set -euo pipefail

echo "[entrypoint] waiting for postgres..."
python - <<'PYEOF'
import os
import sys
import time

import psycopg

deadline = time.time() + 60
last_err = None
while time.time() < deadline:
    try:
        conn = psycopg.connect(
            dbname=os.environ["POSTGRES_DB"],
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            host=os.environ["POSTGRES_HOST"],
            port=os.environ.get("POSTGRES_PORT", "5432"),
            connect_timeout=3,
        )
        conn.close()
        print("[entrypoint] postgres is up.")
        sys.exit(0)
    except Exception as exc:  # noqa: BLE001 - genuinely want to retry on anything here
        last_err = exc
        time.sleep(2)

print(f"[entrypoint] postgres never became reachable: {last_err}", file=sys.stderr)
sys.exit(1)
PYEOF

if [ "${SERVICE_ROLE:-web}" = "web" ]; then
    echo "[entrypoint] running migrations..."
    python manage.py migrate --noinput
    echo "[entrypoint] collecting static files..."
    python manage.py collectstatic --noinput
fi

echo "[entrypoint] starting: $*"
exec "$@"
