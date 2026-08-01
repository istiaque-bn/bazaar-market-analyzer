def market_nav(request):
    from django.conf import settings
    from django.utils import timezone

    from market.models import AnalysisResult, MarketSnapshot, PriceHistory, Stock
    from market.services.market_hours import both_exchanges_status
    from market.services.screener import screen_summary

    def pack(stocks):
        quotes = []
        for s in stocks:
            change = s.last_change_pct
            if change is None:
                recent = list(
                    PriceHistory.objects.live()
                    .filter(stock=s)
                    .order_by("-date")
                    .values_list("close", flat=True)[:2]
                )
                if len(recent) == 2 and recent[1]:
                    change = round((recent[0] / recent[1] - 1) * 100, 2)
            quotes.append(
                {
                    "exchange": s.exchange,
                    "trading_code": s.trading_code,
                    "last_price": s.last_price,
                    "last_change_pct": change,
                    "company_name": s.company_name,
                }
            )
        return quotes

    dse_stocks = list(
        Stock.objects.filter(exchange="DSE", is_active=True, last_price__isnull=False).order_by(
            "-last_volume"
        )[:60]
    )
    cse_stocks = list(
        Stock.objects.filter(exchange="CSE", is_active=True, last_price__isnull=False).order_by(
            "-last_volume"
        )[:60]
    )

    latest_snaps = {}
    for snap in MarketSnapshot.objects.order_by("-as_of"):
        latest_snaps.setdefault(snap.exchange, snap)

    local_now = timezone.localtime()
    return {
        "ticker_quotes_dse": pack(dse_stocks),
        "ticker_quotes_cse": pack(cse_stocks),
        "ticker_snap_dse": latest_snaps.get("DSE"),
        "ticker_snap_cse": latest_snaps.get("CSE"),
        "market_hours": both_exchanges_status(),
        "nav_summary": screen_summary() if AnalysisResult.objects.exists() else None,
        "project_timezone": settings.TIME_ZONE,
        "local_now": local_now,
        "local_now_display": local_now.strftime("%a %d %b · %I:%M %p").lstrip("0").replace(" 0", " "),
    }
