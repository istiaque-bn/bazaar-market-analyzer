"""
Phase 9: liveness vs. readiness.

Liveness answers "is this process alive enough to route a request" —
it must not touch the database or any other dependency, or an
orchestrator using it to decide whether to kill/restart the container
would end up restarting a perfectly healthy process just because the
database happened to be briefly unavailable (the readiness check exists
precisely to signal *that* separately, without triggering a restart).

Readiness answers "can this process actually serve real traffic right
now" by checking the dependencies it can't function without. Per-check
results are boolean-only in the HTTP response — no exception text,
connection strings, or stack traces — full detail goes to the
structured (redacted) log instead, which only staff/ops can read.
"""
from __future__ import annotations

import logging

from django.db import connection

logger = logging.getLogger(__name__)


def check_database() -> bool:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return True
    except Exception:
        logger.exception("Readiness check: database is not reachable")
        return False


def check_broker() -> bool:
    """Best-effort Redis/Celery-broker ping with a short timeout — a slow
    or hung broker must not make the readiness check itself hang."""
    from django.conf import settings

    try:
        import redis

        client = redis.Redis.from_url(settings.CELERY_BROKER_URL, socket_connect_timeout=2, socket_timeout=2)
        return bool(client.ping())
    except Exception:
        logger.exception("Readiness check: broker is not reachable")
        return False


def readiness_checks() -> dict[str, bool]:
    return {"database": check_database(), "broker": check_broker()}
