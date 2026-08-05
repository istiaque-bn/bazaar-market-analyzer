"""Daily screener — ranks potential shares across currently-enabled exchanges."""
from __future__ import annotations

from django.db.models import QuerySet
from django.utils import timezone

from market.models import AnalysisResult, SignalAction, Stock
from market.services.exchange_config import enabled_exchanges


def _latest_as_of():
    """Most recent analysis date on record (DSE/CSE trade Sun-Thu, so
    "today" often has no rows on Fri/Sat or holidays)."""
    latest = AnalysisResult.objects.order_by("-as_of").values_list("as_of", flat=True).first()
    return latest or timezone.localdate()


def potential_shares(limit: int = 20, exchange: str | None = None, min_score: float = 25) -> QuerySet:
    as_of = _latest_as_of()
    # Always intersected with enabled_exchanges() — a disabled exchange
    # never appears in ranking/discovery output, even if a caller (e.g. a
    # stock-list page's own ?exchange= query param) explicitly asks for it.
    qs = AnalysisResult.objects.filter(
        as_of=as_of, score__gte=min_score, stock__exchange__in=enabled_exchanges()
    ).select_related("stock")
    if exchange:
        qs = qs.filter(stock__exchange=exchange)
    return qs.order_by("-score", "-confidence")[:limit]


def safe_buys(limit: int = 10, exchange: str | None = None) -> QuerySet:
    as_of = _latest_as_of()
    qs = AnalysisResult.objects.filter(
        as_of=as_of, is_safe_buy=True, action=SignalAction.BUY, stock__exchange__in=enabled_exchanges()
    ).select_related("stock")
    if exchange:
        qs = qs.filter(stock__exchange=exchange)
    return qs.order_by("-score")[:limit]


def sell_candidates(limit: int = 10, exchange: str | None = None) -> QuerySet:
    as_of = _latest_as_of()
    qs = AnalysisResult.objects.filter(
        as_of=as_of, action=SignalAction.SELL, stock__exchange__in=enabled_exchanges()
    ).select_related("stock")
    if exchange:
        qs = qs.filter(stock__exchange=exchange)
    return qs.order_by("score")[:limit]


def screen_summary() -> dict:
    as_of = _latest_as_of()
    qs = AnalysisResult.objects.filter(as_of=as_of, stock__exchange__in=enabled_exchanges())
    return {
        "as_of": as_of,
        "total": qs.count(),
        "buy": qs.filter(action=SignalAction.BUY).count(),
        "sell": qs.filter(action=SignalAction.SELL).count(),
        "hold": qs.filter(action=SignalAction.HOLD).count(),
        "watch": qs.filter(action=SignalAction.WATCH).count(),
        # Presentation-layer name only; the underlying model field/function
        # (is_safe_buy / safe_buys()) is unchanged to avoid a migration.
        "research_candidates": qs.filter(is_safe_buy=True).count(),
    }


def top_by_sector(limit_per_sector: int = 3) -> dict[str, list]:
    as_of = _latest_as_of()
    results = (
        AnalysisResult.objects.filter(as_of=as_of, score__gte=20, stock__exchange__in=enabled_exchanges())
        .select_related("stock")
        .order_by("stock__sector", "-score")
    )
    buckets: dict[str, list] = {}
    for r in results:
        sector = r.stock.sector or "Other"
        buckets.setdefault(sector, [])
        if len(buckets[sector]) < limit_per_sector:
            buckets[sector].append(r)
    return buckets
