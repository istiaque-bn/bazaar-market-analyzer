from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from datetime import datetime
import json

from market.models import AnalysisResult, BacktestRun, MarketSnapshot, PatternHit, Stock, TechnicalSnapshot, Watchlist
from market.services.indicators import prices_to_df
from market.services.predictor import CONFIDENCE_SCALE, predict_price_at_date
from market.services.screener import potential_shares, safe_buys, screen_summary, sell_candidates, top_by_sector
from notifications.models import Alert


def home(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    return render(request, "market/home.html")


def dashboard(request):
    summary = screen_summary()
    potentials = list(potential_shares(12))
    safes = list(safe_buys(6))
    sells = list(sell_candidates(6))
    snapshots = MarketSnapshot.objects.order_by("-as_of")[:4]
    backtests = BacktestRun.objects.order_by("-created_at")[:4]
    sectors = top_by_sector(2)
    alerts = []
    if request.user.is_authenticated:
        alerts = list(Alert.objects.filter(Q(user=request.user) | Q(user__isnull=True))[:8])
    close_learn = None
    try:
        from market.services.close_learn import learn_status

        close_learn = learn_status()
    except Exception:
        close_learn = None
    return render(
        request,
        "market/dashboard.html",
        {
            "summary": summary,
            "potentials": potentials,
            "safes": safes,
            "sells": sells,
            "snapshots": snapshots,
            "backtests": backtests,
            "sectors": sectors,
            "alerts": alerts,
            "close_learn": close_learn,
        },
    )


def stock_list(request):
    exchange = request.GET.get("exchange", "")
    q = request.GET.get("q", "").strip()
    action = request.GET.get("action", "")
    stocks = Stock.objects.filter(is_active=True)
    if exchange:
        stocks = stocks.filter(exchange=exchange)
    if q:
        stocks = stocks.filter(Q(trading_code__icontains=q) | Q(company_name__icontains=q))
    analyses = {}
    for a in AnalysisResult.objects.filter(stock__in=stocks).select_related("stock").order_by("-as_of"):
        analyses.setdefault(a.stock_id, a)
    if action:
        stocks = [s for s in stocks if analyses.get(s.id) and analyses[s.id].action == action]
    else:
        stocks = list(stocks[:200])
    rows = [(s, analyses.get(s.id)) for s in stocks]
    return render(
        request,
        "market/stock_list.html",
        {"rows": rows, "exchange": exchange, "q": q, "action": action},
    )


HISTORY_RANGES = {
    "3d": {"label": "3 days", "days": 3},
    "7d": {"label": "7 days", "days": 7},
    "15d": {"label": "15 days", "days": 15},
    "30d": {"label": "30 days", "days": 30},
    "3m": {"label": "3 months", "days": 90},
    "6m": {"label": "6 months", "days": 180},
    "1y": {"label": "1 year", "days": 365},
    "2y": {"label": "2 years", "days": 730},
    "5y": {"label": "5 years", "days": 1825},
}

OVERVIEW_RANGES = {
    "3d": {"label": "3 days", "days": 3},
    "7d": {"label": "7 days", "days": 7},
    "15d": {"label": "15 days", "days": 15},
    "30d": {"label": "30 days", "days": 30},
    "3m": {"label": "3 months", "days": 90},
    "6m": {"label": "6 months", "days": 180},
    "1y": {"label": "1 year", "days": 365},
}


def _history_rows_for_stock(stock, range_key: str = "30d"):
    """Return day-by-day OHLC rows + chart payload for the selected lookback."""
    from datetime import timedelta

    meta = HISTORY_RANGES.get(range_key) or HISTORY_RANGES["30d"]
    range_key = range_key if range_key in HISTORY_RANGES else "30d"
    latest = stock.prices.order_by("-date").values_list("date", flat=True).first()
    if not latest:
        return range_key, meta["label"], [], []

    start = latest - timedelta(days=meta["days"] - 1)
    prices = list(stock.prices.filter(date__gte=start, date__lte=latest).order_by("date"))
    rows = []
    chart = []
    prev_close = None
    for p in prices:
        change = None
        change_pct = None
        if prev_close and prev_close > 0:
            change = round(p.close - prev_close, 2)
            change_pct = round((p.close / prev_close - 1) * 100, 2)
        rows.append(
            {
                "date": p.date,
                "open": p.open,
                "high": p.high,
                "low": p.low,
                "close": p.close,
                "volume": p.volume,
                "change": change,
                "change_pct": change_pct,
            }
        )
        chart.append(
            {
                "date": p.date.isoformat(),
                "open": round(float(p.open), 2),
                "high": round(float(p.high), 2),
                "low": round(float(p.low), 2),
                "close": round(float(p.close), 2),
                "volume": int(p.volume or 0),
            }
        )
        prev_close = p.close
    # Table newest-first is easier to scan; charts keep chronological order
    table_rows = list(reversed(rows))
    return range_key, meta["label"], table_rows, chart


def stock_detail(request, exchange: str, code: str):
    stock = get_object_or_404(Stock, exchange=exchange.upper(), trading_code=code.upper())
    analysis = AnalysisResult.objects.filter(stock=stock).order_by("-as_of").first()
    tech = TechnicalSnapshot.objects.filter(stock=stock).order_by("-as_of").first()
    patterns = PatternHit.objects.filter(stock=stock).order_by("-as_of", "-strength")[:12]
    range_key = request.GET.get("range", "30d")
    range_key, range_label, history_rows, history_chart = _history_rows_for_stock(stock, range_key)

    # Overview chart: up to 1y so client-side range pills can filter
    df = prices_to_df(stock.prices.all())
    chart = []
    if not df.empty:
        tail = df.tail(280)  # ~1 trading year buffer
        chart = [
            {
                "date": r["date"].strftime("%Y-%m-%d") if hasattr(r["date"], "strftime") else str(r["date"]),
                "close": round(float(r["close"]), 2),
                "volume": int(r["volume"]),
            }
            for _, r in tail.iterrows()
        ]
    overview_range = request.GET.get("overview", "6m")
    if overview_range not in OVERVIEW_RANGES:
        overview_range = "6m"
    in_watchlist = False
    if request.user.is_authenticated:
        wl = Watchlist.objects.filter(user=request.user, name="Default").first()
        in_watchlist = bool(wl and wl.stocks.filter(id=stock.id).exists())

    from market.models import NextDayCloseForecast
    from market.services.close_learn import learn_status

    next_close_fc = (
        NextDayCloseForecast.objects.filter(stock=stock).order_by("-target_date", "-as_of").first()
    )
    return render(
        request,
        "market/stock_detail.html",
        {
            "stock": stock,
            "analysis": analysis,
            "tech": tech,
            "patterns": patterns,
            "chart_json": json.dumps(chart),
            "overview_ranges": OVERVIEW_RANGES,
            "overview_ranges_json": json.dumps(OVERVIEW_RANGES),
            "overview_range": overview_range,
            "overview_range_label": OVERVIEW_RANGES[overview_range]["label"],
            "history_ranges": HISTORY_RANGES,
            "history_range": range_key,
            "history_range_label": range_label,
            "history_rows": history_rows,
            "history_chart_json": json.dumps(history_chart),
            "history_count": len(history_rows),
            "in_watchlist": in_watchlist,
            "confidence_scale": CONFIDENCE_SCALE,
            "next_close_forecast": next_close_fc,
            "close_learn": learn_status(),
        },
    )


def predict_price_view(request, exchange: str, code: str):
    """JSON: probable close on a selected date + confidence."""
    stock = get_object_or_404(Stock, exchange=exchange.upper(), trading_code=code.upper())
    date_str = (request.GET.get("date") or "").strip()
    if not date_str:
        return JsonResponse({"ok": False, "error": "Pass ?date=YYYY-MM-DD", "confidence_scale": CONFIDENCE_SCALE}, status=400)
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return JsonResponse({"ok": False, "error": "Invalid date. Use YYYY-MM-DD.", "confidence_scale": CONFIDENCE_SCALE}, status=400)

    df = prices_to_df(stock.prices.all())
    result = predict_price_at_date(df, target)
    status = 200 if result.get("ok") else 400
    return JsonResponse(result, status=status)


@login_required
@require_POST
def toggle_watchlist(request, exchange: str, code: str):
    stock = get_object_or_404(Stock, exchange=exchange.upper(), trading_code=code.upper())
    wl, _ = Watchlist.objects.get_or_create(user=request.user, name="Default")
    if wl.stocks.filter(id=stock.id).exists():
        wl.stocks.remove(stock)
    else:
        wl.stocks.add(stock)
    return redirect("stock_detail", exchange=exchange.upper(), code=code.upper())


@login_required
def watchlist_view(request):
    wl, _ = Watchlist.objects.get_or_create(user=request.user, name="Default")
    stocks = wl.stocks.all()
    analyses = {}
    for a in AnalysisResult.objects.filter(stock__in=stocks).order_by("-as_of"):
        analyses.setdefault(a.stock_id, a)
    rows = [(s, analyses.get(s.id)) for s in stocks]
    return render(request, "market/watchlist.html", {"rows": rows})


def backtests_view(request):
    # Latest run only per strategy + exchange (avoid stacked duplicate cards)
    seen = set()
    runs = []
    for b in BacktestRun.objects.order_by("-created_at"):
        key = (b.strategy, b.exchange or "", b.name)
        if key in seen:
            continue
        seen.add(key)
        runs.append(b)
        if len(runs) >= 20:
            break
    return render(request, "market/backtests.html", {"runs": runs})


def alerts_view(request):
    if request.user.is_authenticated:
        alerts = Alert.objects.filter(Q(user=request.user) | Q(user__isnull=True))[:50]
    else:
        alerts = Alert.objects.filter(user__isnull=True)[:50]
    return render(request, "market/alerts.html", {"alerts": alerts})


@login_required
@require_POST
def run_pipeline_view(request):
    """Synchronous pipeline trigger for local demo (no Celery required)."""
    from django.contrib import messages
    from market.services.analyzer import fetch_all, run_full_analysis
    from market.services.autosync import exclusive_db_write

    mode = request.POST.get("mode", "analyze")
    try:
        # Wait for autosync to finish so SQLite does not raise "database is locked"
        with exclusive_db_write(blocking=True, timeout=180):
            if mode == "demo":
                from market.services.dse_fetcher import seed_demo_universe

                seed_demo_universe()
                run_full_analysis(train_ml=True)
            elif mode == "fetch":
                # Live + historical OHLC for both exchanges, then analyze
                result = fetch_all(use_demo_if_empty=False, include_history=True)
                run_full_analysis(train_ml=True)
                dse_h = result.get("dse_hist") or {}
                cse_h = result.get("cse_hist") or {}
                messages.success(
                    request,
                    "Live + history saved — "
                    f"DSE {result.get('dse_live', {}).get('count', 0)} quotes, "
                    f"{dse_h.get('bars_saved', 0)} history bars; "
                    f"CSE {result.get('cse_live', {}).get('count', 0)} quotes, "
                    f"{cse_h.get('bars_saved', 0)} history bars.",
                )
                return redirect("dashboard")
            else:
                run_full_analysis(train_ml=True)
        messages.success(request, "Pipeline finished successfully.")
    except TimeoutError:
        messages.error(request, "Market sync is busy — wait a few seconds and try again.")
    except Exception as exc:
        messages.error(request, f"Pipeline failed: {exc}")
    return redirect("dashboard")


def health(request):
    from django.conf import settings

    local_now = timezone.localtime()
    payload = {
        "status": "ok",
        "stocks": Stock.objects.count(),
        "analyses": AnalysisResult.objects.count(),
        "as_of": str(timezone.localdate()),
        "timezone": settings.TIME_ZONE,
        "local_time": local_now.isoformat(),
        "local_time_display": local_now.strftime("%Y-%m-%d %H:%M:%S %Z"),
    }
    try:
        from market.services.close_learn import learn_status

        payload["close_learn"] = learn_status()
    except Exception as exc:
        payload["close_learn"] = {"error": str(exc)}
    return JsonResponse(payload)


def ticker_json(request):
    """Lightweight payload for the top market scroll bars; triggers auto-sync if stale."""
    from market.services.autosync import get_sync_status, maybe_sync
    from market.services.market_hours import both_exchanges_status
    import threading

    force = request.GET.get("refresh") in ("1", "true", "yes")
    threading.Thread(target=maybe_sync, kwargs={"force": force}, daemon=True).start()

    def pack(qs):
        out = []
        for s in qs:
            change = s.last_change_pct
            if change is None:
                recent = list(s.prices.order_by("-date").values_list("close", flat=True)[:2])
                if len(recent) == 2 and recent[1]:
                    change = round((recent[0] / recent[1] - 1) * 100, 2)
            out.append(
                {
                    "exchange": s.exchange,
                    "trading_code": s.trading_code,
                    "last_price": s.last_price,
                    "last_change_pct": change,
                }
            )
        return out

    dse = list(
        Stock.objects.filter(exchange="DSE", is_active=True, last_price__isnull=False).order_by(
            "-last_volume"
        )[:70]
    )
    cse = list(
        Stock.objects.filter(exchange="CSE", is_active=True, last_price__isnull=False).order_by(
            "-last_volume"
        )[:70]
    )

    snapshots = {}
    for snap in MarketSnapshot.objects.order_by("-as_of"):
        if snap.exchange in snapshots:
            continue
        snapshots[snap.exchange] = {
            "exchange": snap.exchange,
            "index_value": snap.index_value,
            "index_change_pct": snap.index_change_pct,
            "as_of": str(snap.as_of),
            "notes": snap.notes,
        }

    return JsonResponse(
        {
            "dse": {
                "quotes": pack(dse),
                "index": snapshots.get("DSE"),
            },
            "cse": {
                "quotes": pack(cse),
                "index": snapshots.get("CSE"),
            },
            "quotes": pack(dse) + pack(cse),
            "indexes": list(snapshots.values()),
            "sync": get_sync_status(),
            "market_hours": both_exchanges_status(),
        }
    )


