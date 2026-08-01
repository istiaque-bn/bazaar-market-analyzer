"""Minimal fixed-window rate limiter for plain Django views (non-DRF).

Uses Django's cache framework, so it works with whatever CACHES backend is
configured. With the default per-process LocMemCache, limits are enforced
per worker process rather than globally across a multi-worker deployment —
acceptable for a first pass; point CACHES at a shared backend (e.g. Redis,
already used for Celery) for a process-wide limit.
"""
from __future__ import annotations

from django.core.cache import cache


def is_rate_limited(key: str, limit: int, period_seconds: int) -> bool:
    """Return True if `key` has been used more than `limit` times within
    the current `period_seconds` window."""
    cache_key = f"ratelimit:{key}"
    try:
        count = cache.incr(cache_key)
    except ValueError:
        cache.set(cache_key, 1, timeout=period_seconds)
        count = 1
    return count > limit
