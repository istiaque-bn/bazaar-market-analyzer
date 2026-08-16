"""Dashboard and landing-page views, re-exported by :mod:`market.views`.

Keeping these endpoints here makes the main view facade smaller without
changing URL targets or their authentication contract.
"""
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import redirect, render
from django.utils import timezone

from accounts.roles import role_home_url
from market.models import BacktestRun, MarketSnapshot
from market.services.autosync import get_last_success_at
from market.services.exchange_config import enabled_exchanges
from market.services.market_hours import session_status
from market.services.ops_alerts import STALE_DATA_DAYS, recent_silent_sync_error
from market.services.screener import potential_shares, safe_buys, screen_summary, sell_candidates, sentiment_label, top_by_sector
from market.services.signal_status import market_edge_status
from notifications.models import Alert


def home(request):
    """Show the public product page, or a user's role-specific panel."""
    if request.user.is_authenticated:
        return redirect(role_home_url(request.user))
    return render(request, "market/landing.html")


def _dashboard_health_issue(as_of) -> str | None:
    """Return the cheap staff-only freshness warning for the dashboard."""
    today = timezone.localdate()
    if as_of and (today - as_of).days > STALE_DATA_DAYS:
        return f"Signals are {(today - as_of).days} days old (last analyzed {as_of}) — the pipeline may not be running."
    error = recent_silent_sync_error("market.tasks.sync_live_market")
    if error:
        return f"Live sync is silently failing: {error[:150]}"
    return None


@login_required
def dashboard(request):
    enabled = enabled_exchanges()
    summary = screen_summary()
    potentials = list(potential_shares(12))
    safes = list(safe_buys(6))
    sells = list(sell_candidates(6))
    snapshots = MarketSnapshot.objects.filter(exchange__in=enabled).order_by("-as_of")[:4]
    sectors = top_by_sector(2)
    sentiment = []
    for exchange in enabled:
        snapshot = MarketSnapshot.objects.filter(exchange=exchange).order_by("-as_of").first()
        if snapshot:
            row = sentiment_label(snapshot.advancers, snapshot.decliners, snapshot.unchanged)
            row["exchange"] = exchange
            row["as_of"] = snapshot.as_of
            sentiment.append(row)
    backtests = BacktestRun.objects.filter(Q(exchange__in=enabled) | Q(exchange="") | Q(exchange__isnull=True)).order_by("-created_at")[:4]
    alerts = []
    if request.user.is_authenticated:
        alerts = list(Alert.objects.filter(Q(user=request.user) | Q(user__isnull=True))[:8])
    try:
        from market.services.close_learn import learn_status

        close_learn = learn_status()
    except Exception:
        close_learn = None
    try:
        edge = market_edge_status()
    except Exception:
        edge = {"has_edge": False, "edge_reason": "Model status unavailable."}
    return render(
        request,
        "market/dashboard.html",
        {
            "summary": summary,
            "potentials": potentials,
            "safes": safes,
            "sells": sells,
            "snapshots": snapshots,
            "sentiment": sentiment,
            "backtests": backtests,
            "sectors": sectors,
            "alerts": alerts,
            "close_learn": close_learn,
            "edge": edge,
            "data_last_updated": get_last_success_at(),
            "dse_session": session_status("DSE"),
            "health_issue": _dashboard_health_issue(summary.get("as_of")) if request.user.is_staff else None,
        },
    )
