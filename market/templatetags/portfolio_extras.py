"""Template helpers for BDT currency formatting and gain/loss coloring —
kept as plain filters (no django.contrib.humanize dependency) so a
None/missing value renders as "—" instead of "0.00", which for a missing
price or empty cost basis is a meaningfully different, more honest thing
to show than a fabricated zero."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


def _to_decimal(value):
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


@register.filter
def bdt(value, decimals: int = 2):
    """Decimal/str/float -> "৳1,23,456.78" using the Bangladeshi 2-digit
    lakh grouping (1,23,456 not 123,456), or "—" for None."""
    d = _to_decimal(value)
    if d is None:
        return "—"
    quant = Decimal(1).scaleb(-int(decimals))
    d = d.quantize(quant)
    sign = "-" if d < 0 else ""
    d = abs(d)
    int_part, _, frac_part = f"{d:f}".partition(".")
    if len(int_part) > 3:
        last3 = int_part[-3:]
        rest = int_part[:-3]
        groups = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        int_part = ",".join(groups) + "," + last3
    grouped = int_part + (f".{frac_part}" if decimals else "")
    return f"৳{sign}{grouped}"


@register.filter
def pl_class(value):
    """"up"/"down"/"" — matches the .up/.down colors already used across
    the ticker and dashboard, so gains/losses read consistently
    everywhere in the app."""
    d = _to_decimal(value)
    if d is None or d == 0:
        return ""
    return "up" if d > 0 else "down"


@register.filter
def signed(value, decimals: int = 2):
    """Prefix a positive number with "+" — "+1,250.00" / "-340.00" — for
    P/L figures where the sign itself is the first thing worth reading."""
    d = _to_decimal(value)
    if d is None:
        return "—"
    quant = Decimal(1).scaleb(-int(decimals))
    d = d.quantize(quant)
    return f"+{d}" if d > 0 else str(d)
