# Exact url_name membership per navbar group — used for active-state
# highlighting so a stock-detail page keeps "Stocks" active, a portfolio
# sub-page keeps "Portfolio" active, etc. Deliberately a set-membership
# check (not a substring/`in` string test) so a url_name can never
# accidentally match a sibling group's name.
NAV_GROUPS = {
    "panel": {"admin_panel", "staff_panel", "user_panel"},
    "accounts": {
        "account_list",
        "account_detail",
        "account_create_user",
        "account_create_staff",
    },
    "stocks": {"stock_list", "stock_detail", "predict_price"},
    "portfolio": {
        "portfolio_redirect",
        "portfolio_list",
        "portfolio_create",
        "portfolio_detail",
        "portfolio_rename",
        "portfolio_set_default",
        "portfolio_delete",
        "portfolio_transactions",
        "portfolio_add_transaction",
        "portfolio_add_holding",
        "portfolio_edit_transaction",
        "portfolio_delete_transaction",
    },
    "watchlist": {"watchlist"},
    "feedback": {
        "feedback_my_list",
        "feedback_submit",
        "feedback_detail",
        "feedback_triage_list",
        "feedback_admin_dashboard",
    },
    "tools": {"dashboard", "backtests", "alerts"},
    "admin": {"data_quality", "ops_report", "ml_reliability"},
}


def _active_nav(request):
    match = getattr(request, "resolver_match", None)
    url_name = getattr(match, "url_name", None) if match else None
    for group, names in NAV_GROUPS.items():
        if url_name in names:
            return group
    return None


def market_nav(request):
    from django.conf import settings
    from django.utils import timezone

    from market.models import AnalysisResult, Exchange, MarketSnapshot, PriceHistory, Stock
    from market.services.exchange_config import enabled_exchanges
    from market.services.market_hours import both_exchanges_status
    from market.services.autosync import get_last_success_at
    from market.services.screener import screen_summary

    enabled = enabled_exchanges()
    dse_on = Exchange.DSE in enabled
    cse_on = Exchange.CSE in enabled

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

    dse_stocks = (
        list(
            Stock.objects.filter(exchange="DSE", is_active=True, last_price__isnull=False).order_by(
                "-last_volume"
            )[:60]
        )
        if dse_on
        else []
    )
    cse_stocks = (
        list(
            Stock.objects.filter(exchange="CSE", is_active=True, last_price__isnull=False).order_by(
                "-last_volume"
            )[:60]
        )
        if cse_on
        else []
    )

    latest_snaps = {}
    for snap in MarketSnapshot.objects.filter(exchange__in=enabled).order_by("-as_of"):
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
        # This is the last completed market-sync time, not the page-render
        # time, so every page can state exactly how fresh its prices are.
        "market_data_updated_at": get_last_success_at(),
        "active_nav": _active_nav(request),
        "enabled_exchanges": enabled,
        "dse_enabled": dse_on,
        "cse_enabled": cse_on,
    }
