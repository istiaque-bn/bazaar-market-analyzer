"""
Single source of truth for which exchanges this deployment actively
fetches, analyzes, trains models for, and exposes to public discovery.

Controlled by the ENABLE_DSE/ENABLE_CSE settings (config/settings/base.py,
which parses the raw ENABLE_DSE/ENABLE_CSE/MAINTENANCE_MODE environment
variables exactly once at process start, and validates that at least one
exchange is enabled unless MAINTENANCE_MODE is set). Every other module in
the project is expected to call enabled_exchanges()/is_exchange_enabled()
from here rather than reading settings.ENABLE_DSE/ENABLE_CSE or
os.environ directly, so this exact policy is expressed in exactly one
place.

Disabling an exchange is a purely operational toggle: it stops new
fetching/analysis/training for that exchange and hides it from public
discovery (stock lists, screener rankings, watchlist-add, new portfolio
purchases), but never deletes, mutates, or relabels any existing row.
Re-enabling (flip the flag, restart services) makes existing data and
scheduled processing for that exchange active again immediately — see
README.md's "Exchange feature flags" section for the full
disable/re-enable/catch-up procedure.

Pure function of settings — no DB queries, no network access, safe to
call from any request path, Celery task, or management command, including
at import time.
"""
from __future__ import annotations

from django.conf import settings

from market.models import Exchange

_ALL_EXCHANGES = (Exchange.DSE, Exchange.CSE)


def enabled_exchanges() -> list[str]:
    """Exchange codes this deployment actively serves, in (DSE, CSE)
    declaration order."""
    flags = {
        Exchange.DSE: getattr(settings, "ENABLE_DSE", True),
        Exchange.CSE: getattr(settings, "ENABLE_CSE", False),
    }
    return [ex for ex in _ALL_EXCHANGES if flags.get(ex, False)]


def disabled_exchanges() -> list[str]:
    """The complement of enabled_exchanges() — exchanges with existing
    data that this deployment is not currently fetching/analyzing for."""
    enabled = set(enabled_exchanges())
    return [ex for ex in _ALL_EXCHANGES if ex not in enabled]


def is_exchange_enabled(exchange: str | None) -> bool:
    return bool(exchange) and exchange in enabled_exchanges()
