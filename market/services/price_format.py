"""
DSE/CSE traded prices only ever land on real 0.10-taka tick increments —
every actual close in this dataset ends in X.Y0 (see any stock's OHLCV
table). A raw model output like 16.83 can't be a real future price, so
predicted prices are snapped to the same tick grid before display. This is
a display/rounding step only — it doesn't change what the model predicted,
just how it's shown.
"""
from __future__ import annotations

TICK_SIZE = 0.10


def round_to_tick(price, tick: float = TICK_SIZE):
    """Round `price` to the nearest tick (default 0.10), or None through."""
    if price is None:
        return None
    return round(round(float(price) / tick) * tick, 2)
