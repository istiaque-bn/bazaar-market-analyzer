"""Cross-process distributed locking (Redis-backed).

A Python threading.Lock only protects threads inside a single process — it
gives zero protection once background work runs as separate Celery worker
processes (or multiple worker machines). This uses Redis (already the
Celery broker, already a project dependency) as the shared lock store via
the standard SET NX PX / compare-and-delete pattern.
"""
from __future__ import annotations

import contextlib
import time
import uuid

import redis
from django.conf import settings

_client: redis.Redis | None = None

_RELEASE_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


class LockBusy(Exception):
    """Raised when a distributed lock could not be acquired in time."""


def _get_client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(settings.CELERY_BROKER_URL)
    return _client


@contextlib.contextmanager
def distributed_lock(key: str, timeout: float = 300.0, blocking_timeout: float = 0.0):
    """Hold an exclusive, cross-process lock named `key` for up to
    `timeout` seconds (auto-expires so a crashed holder can't wedge it
    forever). Waits up to `blocking_timeout` seconds to acquire before
    raising LockBusy; 0 means fail immediately if already held."""
    client = _get_client()
    token = uuid.uuid4().hex
    lock_key = f"bazaar:lock:{key}"
    deadline = time.monotonic() + blocking_timeout
    acquired = client.set(lock_key, token, nx=True, px=int(timeout * 1000))
    while not acquired and time.monotonic() < deadline:
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())) or 0.01)
        acquired = client.set(lock_key, token, nx=True, px=int(timeout * 1000))
    if not acquired:
        raise LockBusy(f"Could not acquire lock '{key}' within {blocking_timeout}s")
    try:
        yield
    finally:
        try:
            client.eval(_RELEASE_LUA, 1, lock_key, token)
        except Exception:
            pass
