from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from datetime import datetime

from accounts.decorators import admin_required, staff_or_admin_required
from accounts.roles import role_home_url
from market.forms import PortfolioForm, TransactionForm
from market.models import (
    AnalysisResult,
    BacktestRun,
    MarketSnapshot,
    PatternHit,
    Portfolio,
    PortfolioTransaction,
    Stock,
    TechnicalSnapshot,
    TransactionType,
    Watchlist,
)
from market.services.autosync import get_last_success_at
from market.services.ops_alerts import STALE_DATA_DAYS, recent_silent_sync_error
from market.services.indicators import prices_to_df
from market.services.predictor import CONFIDENCE_SCALE, RESEARCH_DISCLAIMER, predict_price_at_date
from market.services.screener import potential_shares, safe_buys, screen_summary, sell_candidates, top_by_sector
from market.services.signal_status import market_edge_status, signal_status
from notifications.models import Alert


def home(request):
    """Anonymous visitors get only Login (+ static assets); there is no
    public marketing page any more — authenticated visitors land on the
    Market dashboard regardless of role (accounts.roles.role_home_url)."""
    if request.user.is_authenticated:
        return redirect(role_home_url(request.user))
    return redirect("login")


@login_required
def dashboard(request):
    from market.services.exchange_config import enabled_exchanges
    from market.services.market_hours import session_status

    enabled = enabled_exchanges()
    summary = screen_summary()
    potentials = list(potential_shares(12))
    safes = list(safe_buys(6))
    sells = list(sell_candidates(6))
    snapshots = MarketSnapshot.objects.filter(exchange__in=enabled).order_by("-as_of")[:4]
    backtests = BacktestRun.objects.filter(Q(exchange__in=enabled) | Q(exchange="") | Q(exchange__isnull=True)).order_by("-created_at")[:4]
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
    try:
        edge = market_edge_status()
    except Exception:
        edge = {"has_edge": False, "edge_reason": "Model status unavailable."}
    health_issue = _dashboard_health_issue(summary.get("as_of")) if request.user.is_staff else None
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
            "edge": edge,
            "data_last_updated": get_last_success_at(),
            "dse_session": session_status("DSE"),
            "health_issue": health_issue,
        },
    )


@admin_required
def paper_trading_view(request):
    from django.db.models import Avg, Count, Q
    from market.models import PaperLearningFeedback
    from market.services.paper_learning import paper_learning_report
    from market.services.paper_trading import DEFAULT_CONFIG, account_summary, ensure_account

    account = ensure_account()
    summary = account_summary(account)
    feedback_stats = PaperLearningFeedback.objects.aggregate(
        count=Count("id"), wins=Count("id", filter=Q(profitable_after_costs=True)), avg_net_return=Avg("net_return_pct")
    )
    feedback_stats["win_rate"] = (
        round(feedback_stats["wins"] / feedback_stats["count"] * 100, 2) if feedback_stats["count"] else None
    )
    positions = account.positions.filter(is_open=True).select_related("stock", "signal")
    position_rows = []
    for position in positions:
        current = position.stock.last_price or float(position.entry_price)
        pnl = (float(current) - float(position.entry_price)) * position.quantity - float(position.entry_fee)
        position_rows.append({"position": position, "current_price": current, "unrealized_pnl": pnl})
    equity_chart = [
        {
            "date": row["as_of"].isoformat(),
            "equity": float(row["total_equity"]),
            "return_pct": round(float((row["total_equity"] / account.initial_cash - 1) * 100), 2) if account.initial_cash else 0,
        }
        for row in account.equity_snapshots.order_by("as_of").values("as_of", "total_equity")[:90]
    ]
    return render(
        request,
        "market/paper_trading.html",
        {
            **summary,
            "position_rows": position_rows,
            "trades": account.trades.select_related("stock", "position")[:50],
            "snapshots": account.equity_snapshots.all()[:30],
            "equity_chart": equity_chart,
            "feedback_stats": feedback_stats,
            "learning_report": paper_learning_report(),
            "paper_config": {**DEFAULT_CONFIG, **(account.strategy_config or {})},
        },
    )


@admin_required
@require_POST
def paper_trading_control(request):
    from market.services.paper_trading import DEFAULT_CONFIG, ensure_account

    account = ensure_account()
    action = request.POST.get("action")
    if action in {"start", "pause"}:
        account.is_active = action == "start"
        account.save(update_fields=["is_active", "updated_at"])
        messages.success(request, "Autonomous paper trading started." if account.is_active else "Autonomous paper trading paused.")
    elif action == "use_book_rules":
        account.strategy_config = dict(DEFAULT_CONFIG)
        account.save(update_fields=["strategy_config", "updated_at"])
        messages.success(request, "Three-day trend paper rules enabled. This remains a virtual-only simulation.")
    elif action == "run":
        from market.tasks import run_paper_trading

        run_paper_trading.delay(force=True)
        messages.success(request, "Paper-trading cycle queued. It will remain virtual and use stored prices only.")
    else:
        messages.error(request, "Unknown paper-trading action.")
    return redirect("paper_trading")


def _dashboard_health_issue(as_of) -> str | None:
    """Cheap, staff-only pre-flight for the dashboard banner — deliberately
    NOT the full ops_alerts.evaluate_alerts()/ops_summary(), since that
    includes a provenance_report() scan over the whole PriceHistory table
    that's too expensive to run on every dashboard load. Just the two
    checks relevant to "is what I'm looking at right now trustworthy":
    is the analysis stale, and is the last sync silently failing."""
    today = timezone.localdate()
    if as_of and (today - as_of).days > STALE_DATA_DAYS:
        return f"Signals are {(today - as_of).days} days old (last analyzed {as_of}) — the pipeline may not be running."
    error = recent_silent_sync_error("market.tasks.sync_live_market")
    if error:
        return f"Live sync is silently failing: {error[:150]}"
    return None


def _get_stock_for_public_route(exchange: str, code: str) -> Stock:
    """Shared lookup for every *public-discovery* stock route (detail page,
    prediction/analysis requests, and their API equivalents) — 404s for a
    stock on a currently-disabled exchange exactly the same way it 404s
    for a stock that plain doesn't exist, so a disabled exchange's pages
    simply aren't part of the site right now rather than existing in some
    intermediate "found but blocked" state. This is deliberately a
    different (more permissive) lookup than watchlist/portfolio access to
    an *already-owned* record — see toggle_watchlist and
    market.services.portfolio, where existing user data must stay
    readable regardless of this check."""
    from django.http import Http404

    from market.services.exchange_config import is_exchange_enabled

    stock = get_object_or_404(Stock, exchange=exchange.upper(), trading_code=code.upper())
    if not is_exchange_enabled(stock.exchange):
        raise Http404("Stock not found.")
    return stock


@login_required
def stock_list(request):
    from market.services.exchange_config import enabled_exchanges
    from market.services.stock_quality import assess_stock_quality

    exchange = request.GET.get("exchange", "")
    q = request.GET.get("q", "").strip()
    action = request.GET.get("action", "")
    stocks = Stock.objects.filter(is_active=True, exchange__in=enabled_exchanges())
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
        stocks = list(stocks)
    quality = assess_stock_quality(stocks)
    main_rows = [(s, analyses.get(s.id), quality[s.id]) for s in stocks if not quality[s.id]["limited"]]
    limited_rows = [(s, analyses.get(s.id), quality[s.id]) for s in stocks if quality[s.id]["limited"]]
    return render(
        request,
        "market/stock_list.html",
        {
            "main_rows": main_rows,
            "limited_rows": limited_rows,
            "exchange": exchange,
            "q": q,
            "action": action,
        },
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
    """Return day-by-day OHLC rows + chart payload for the selected lookback.

    Walks every calendar day in the range (not just days with a row) so that
    weekends/holidays can be labeled instead of silently vanishing from the
    table. A trading day with no row (a genuine data gap) is still skipped,
    same as before — only recognized closures get a placeholder row.
    """
    from datetime import timedelta

    from market.services.trading_calendar import closure_reason

    meta = HISTORY_RANGES.get(range_key) or HISTORY_RANGES["30d"]
    range_key = range_key if range_key in HISTORY_RANGES else "30d"
    latest = stock.prices.order_by("-date").values_list("date", flat=True).first()
    if not latest:
        return range_key, meta["label"], [], []

    start = latest - timedelta(days=meta["days"] - 1)
    prices_by_date = {
        p.date: p for p in stock.prices.filter(date__gte=start, date__lte=latest).order_by("date")
    }
    rows = []
    chart = []
    prev_close = None
    d = start
    while d <= latest:
        p = prices_by_date.get(d)
        if p is not None:
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
                    "closed_reason": None,
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
        else:
            reason = closure_reason(d)
            if reason:
                rows.append(
                    {
                        "date": d,
                        "open": None,
                        "high": None,
                        "low": None,
                        "close": None,
                        "volume": None,
                        "change": None,
                        "change_pct": None,
                        "closed_reason": reason,
                    }
                )
        d += timedelta(days=1)
    # Table newest-first is easier to scan; charts keep chronological order
    table_rows = list(reversed(rows))
    return range_key, meta["label"], table_rows, chart


@login_required
def stock_detail(request, exchange: str, code: str):
    stock = _get_stock_for_public_route(exchange, code)
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
    from market.services.price_format import round_to_tick

    # Split into two distinct rows rather than one "latest of either kind"
    # row: an unsettled forecast (target_date in the future, no actual yet)
    # and a settled one (already resolved against a real close) answer
    # different questions and were previously conflated — whichever had
    # the higher target_date silently won, so a fresh "predicting
    # tomorrow" row would hide the most recent settled comparison, or a
    # stale settled row would hide the fact that no new prediction has
    # been generated in days.
    next_close_pending = (
        NextDayCloseForecast.objects.filter(stock=stock, actual_close__isnull=True).order_by("-target_date").first()
    )
    next_close_settled = (
        NextDayCloseForecast.objects.filter(stock=stock, actual_close__isnull=False).order_by("-target_date").first()
    )
    next_close_pending_price = round_to_tick(next_close_pending.predicted_close) if next_close_pending else None
    next_close_settled_price = round_to_tick(next_close_settled.predicted_close) if next_close_settled else None
    try:
        status = signal_status(stock, analysis, tech)
    except Exception:
        status = None
    return render(
        request,
        "market/stock_detail.html",
        {
            "stock": stock,
            "analysis": analysis,
            "tech": tech,
            "patterns": patterns,
            # Passed as plain Python objects, not json.dumps() strings — the
            # template embeds them via the `json_script` filter (HTML/script-
            # safe serialization) rather than `|safe`, so nothing here can
            # break out of its <script> tag or inject markup.
            "chart_data": chart,
            "history_chart_data": history_chart,
            "overview_ranges": OVERVIEW_RANGES,
            "overview_range": overview_range,
            "overview_range_label": OVERVIEW_RANGES[overview_range]["label"],
            "history_ranges": HISTORY_RANGES,
            "history_range": range_key,
            "history_range_label": range_label,
            "history_rows": history_rows,
            "history_count": len(history_rows),
            "in_watchlist": in_watchlist,
            "confidence_scale": CONFIDENCE_SCALE,
            "next_close_pending": next_close_pending,
            "next_close_pending_price": next_close_pending_price,
            "next_close_settled": next_close_settled,
            "next_close_settled_price": next_close_settled_price,
            "close_learn": learn_status(),
            "status": status,
        },
    )


@login_required
def predict_price_view(request, exchange: str, code: str):
    """JSON: probable close on a selected date + confidence. A new
    prediction request for a disabled exchange 404s the same as the
    stock-detail page — see _get_stock_for_public_route."""
    stock = _get_stock_for_public_route(exchange, code)
    date_str = (request.GET.get("date") or "").strip()
    if not date_str:
        return JsonResponse({"ok": False, "error": "Pass ?date=YYYY-MM-DD", "confidence_scale": CONFIDENCE_SCALE}, status=400)
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return JsonResponse({"ok": False, "error": "Invalid date. Use YYYY-MM-DD.", "confidence_scale": CONFIDENCE_SCALE}, status=400)

    df = prices_to_df(stock.prices.all())
    result = predict_price_at_date(df, target)
    result.setdefault("disclaimer", RESEARCH_DISCLAIMER)
    status = 200 if result.get("ok") else 400
    return JsonResponse(result, status=status)


@login_required
@require_POST
def toggle_watchlist(request, exchange: str, code: str):
    """Looked up directly (not via _get_stock_for_public_route) since
    removing an existing watchlist entry must keep working even for a
    stock whose exchange has since been disabled — only *adding* a new
    disabled-exchange stock is refused. In practice a disabled exchange's
    stock-detail "add" button is itself unreachable (that page 404s), so
    the add-while-disabled branch below only matters as a server-side
    guard against a stale link or a direct POST."""
    from market.services.exchange_config import is_exchange_enabled

    stock = get_object_or_404(Stock, exchange=exchange.upper(), trading_code=code.upper())
    wl, _ = Watchlist.objects.get_or_create(user=request.user, name="Default")
    already_watching = wl.stocks.filter(id=stock.id).exists()
    if already_watching:
        wl.stocks.remove(stock)
    elif not is_exchange_enabled(stock.exchange):
        messages.error(
            request,
            f"{stock.exchange} is currently disabled for this deployment — new watchlist additions aren't available.",
        )
        return redirect("watchlist")
    else:
        wl.stocks.add(stock)
    if is_exchange_enabled(stock.exchange):
        return redirect("stock_detail", exchange=exchange.upper(), code=code.upper())
    return redirect("watchlist")


@login_required
def watchlist_view(request):
    wl, _ = Watchlist.objects.get_or_create(user=request.user, name="Default")
    stocks = wl.stocks.all()
    analyses = {}
    for a in AnalysisResult.objects.filter(stock__in=stocks).order_by("-as_of"):
        analyses.setdefault(a.stock_id, a)
    rows = [(s, analyses.get(s.id)) for s in stocks]
    return render(request, "market/watchlist.html", {"rows": rows})


@login_required
def backtests_view(request):
    from market.services.exchange_config import enabled_exchanges

    # Latest run only per strategy + exchange (avoid stacked duplicate cards).
    # A run with no exchange (blank/None) is a non-exchange-specific backtest
    # and always shown; an exchange-scoped run only shows if that exchange
    # is currently enabled — a disabled exchange's ranking/summary content
    # stays hidden from this public results page, same as the dashboard.
    enabled = enabled_exchanges()
    seen = set()
    runs = []
    for b in BacktestRun.objects.order_by("-created_at"):
        if b.exchange and b.exchange not in enabled:
            continue
        key = (b.strategy, b.exchange or "", b.name)
        if key in seen:
            continue
        seen.add(key)
        runs.append(b)
        if len(runs) >= 20:
            break
    return render(request, "market/backtests.html", {"runs": runs})


@login_required
def alerts_view(request):
    alerts = Alert.objects.filter(Q(user=request.user) | Q(user__isnull=True))[:50]
    return render(request, "market/alerts.html", {"alerts": alerts})


@staff_or_admin_required
def data_quality_view(request):
    """Admin + Staff data-quality report: provenance breakdown, freshness
    per exchange, flagged-row counts, and recent import batch health —
    this is exactly the "DSE freshness ... task status" content Staff
    are explicitly granted (see accounts/roles.py's role hierarchy)."""
    from market.services.data_quality import provenance_report

    return render(request, "market/data_quality.html", {"report": provenance_report()})


@staff_or_admin_required
def ops_report_view(request):
    """Admin + Staff operational readiness report (Phase 9): task health,
    prediction volume, rejected-row/freshness summary, model drift, and
    any currently-firing alert-threshold breaches."""
    from market.services.ops_alerts import evaluate_alerts
    from market.services.ops_metrics import ops_summary

    summary = ops_summary()
    alerts = evaluate_alerts(summary)
    critical_count = sum(1 for a in alerts if a["severity"] == "critical")
    return render(
        request,
        "market/ops_report.html",
        {"summary": summary, "alerts": alerts, "critical_count": critical_count},
    )


@admin_required
def ml_reliability_view(request):
    """Admin-only: ML Reliability Monitor dashboard: current status per
    deployed model/exchange/horizon/window, skill vs. baseline,
    calibration, drift warnings, economic diagnostics, recommendations,
    and assessment history."""
    from django.conf import settings

    from market.models import MLModelVersion, TaskRun, TaskStatus
    from market.services.exchange_config import enabled_exchanges
    from market.services.reliability_report import assessment_history, latest_assessments

    last_training = TaskRun.objects.filter(task_name="market.tasks.train_ml_model").order_by("-started_at").first()
    training_message = "No automatic ML training check has been recorded yet."
    if last_training:
        skipped_reason = (last_training.detail or {}).get("skipped")
        if last_training.status == TaskStatus.SUCCESS:
            training_message = "Training completed successfully. A new model may be active if it passed the quality checks."
        elif last_training.status == TaskStatus.SKIPPED and skipped_reason == "no new label-resolvable data since last train":
            training_message = (
                "Normal skip: there is no new 10-trading-day outcome to learn from yet. "
                "The system will check again automatically."
            )
        elif last_training.status == TaskStatus.SKIPPED and skipped_reason == "disabled":
            training_message = "Automatic ML training is turned off in the server settings."
        elif last_training.status == TaskStatus.SKIPPED and skipped_reason == "already_running":
            training_message = "Another ML training run was already in progress, so this duplicate check was safely skipped."
        elif last_training.status == TaskStatus.SKIPPED:
            training_message = "This check was safely skipped: " + (str(skipped_reason) if skipped_reason else "no work was needed.")
        elif last_training.status == TaskStatus.FAILURE:
            training_message = "Training failed. Check the error message below and retry only after fixing the cause."
        elif last_training.status == TaskStatus.STARTED:
            training_message = "Training is currently running."

    enabled = set(enabled_exchanges())
    groups = [
        {
            "latest": a,
            "history": assessment_history(a.model_family, a.exchange, a.horizon_trading_days, a.window_label, limit=10)[1:],
        }
        for a in latest_assessments()
        if a.exchange in enabled
    ]
    for index, group in enumerate(groups, start=1):
        assessments = list(reversed(group["history"])) + [group["latest"]]
        points = []
        metric_label = ""
        for assessment in assessments:
            metrics = assessment.metrics or {}
            classification = metrics.get("classification") or {}
            regression = metrics.get("regression") or {}
            if classification.get("model", {}).get("balanced_accuracy") is not None:
                metric_label = "Balanced accuracy"
                value = classification["model"]["balanced_accuracy"]
            elif regression.get("model", {}).get("direction_hit_rate") is not None:
                metric_label = "Direction hit rate"
                value = regression["model"]["direction_hit_rate"]
            else:
                continue
            points.append({"date": assessment.run_at.date().isoformat(), "value": float(value)})
        group["chart"] = {"label": metric_label, "points": points, "id": f"ml-chart-data-{index}"}
    model_labels = {
        "forward_return_rf": "10-Day Price Direction",
        "next_close_rf": "Next-Day Price Change",
    }
    active_models = list(
        MLModelVersion.objects.filter(is_active=True, exchange_scope__in=enabled).order_by("model_name", "exchange_scope")
    )
    for model in active_models:
        model.display_name = model_labels.get(model.model_name, model.model_name)

    return render(
        request,
        "market/ml_reliability.html",
        {
            "groups": groups,
            "training": {
                "enabled": getattr(settings, "AUTO_ML_TRAINING", True),
                "schedule": getattr(settings, "AUTO_ML_TRAINING_TIME", "00:30"),
                "last_run": last_training,
                "message": training_message,
                "active_models": active_models,
            },
        },
    )


@admin_required
@require_POST
def run_pipeline_view(request):
    """Enqueue a pipeline job — Admin-only ("Manage DSE pipeline and
    training controls" is an Admin capability, not Staff's — see
    accounts/roles.py), POST + CSRF protected.

    Fetch/analysis/training jobs hit external upstreams and can retrain
    the ML model, so ordinary users must not be able to trigger them, and
    web requests must not block on this work — it runs on a Celery
    worker, not the request thread."""
    from celery import chain
    from django.contrib import messages

    from market.models import AdminAuditAction
    from market.services.audit import record_admin_action
    from market.tasks import fetch_all_market_data, run_full_analysis_task, seed_demo_and_analyze

    from market.tasks import (
        run_end_of_day_pipeline,
        run_intraday_analysis,
        train_ml_model,
    )

    mode = request.POST.get("mode", "analyze")
    try:
        if mode == "demo":
            seed_demo_and_analyze.delay()
            messages.success(request, "Demo seed + analysis queued.")
        elif mode == "fetch":
            chain(fetch_all_market_data.s(include_history=True), run_full_analysis_task.si(train_ml=True)).delay()
            messages.success(request, "Live + history fetch, then analysis, queued.")
        elif mode == "quote":
            fetch_all_market_data.delay(include_history=False)
            messages.success(request, "Live quote fetch queued.")
        elif mode == "intraday":
            run_intraday_analysis.delay()
            messages.success(request, "Lightweight intraday analysis queued.")
        elif mode == "eod":
            run_end_of_day_pipeline.delay()
            messages.success(request, "End-of-day pipeline queued.")
        elif mode == "train":
            if request.POST.get("confirm") != "yes":
                messages.error(request, "Training a new model is an expensive operation — confirmation is required.")
                record_admin_action(request, AdminAuditAction.PIPELINE_TRIGGERED, {"mode": mode, "error": "not confirmed"})
                return redirect("admin_panel")
            train_ml_model.delay()
            messages.success(request, "ML model training queued.")
        else:
            run_full_analysis_task.delay(train_ml=True)
            messages.success(request, "Re-analysis queued.")
        record_admin_action(request, AdminAuditAction.PIPELINE_TRIGGERED, {"mode": mode})
    except Exception as exc:
        messages.error(request, f"Could not queue pipeline job: {exc}")
        record_admin_action(request, AdminAuditAction.PIPELINE_TRIGGERED, {"mode": mode, "error": str(exc)[:500]})
    # Fixed, not user-supplied: the new automation-control modes are only
    # ever submitted from the Admin panel and should return there; the
    # original modes (demo/fetch/analyze) keep their pre-existing
    # dashboard redirect so existing behavior/tests are unaffected.
    return redirect("admin_panel" if mode in ("intraday", "eod", "train") else "dashboard")


_RETRYABLE_TASKS = (
    "market.tasks.sync_live_market",
    "market.tasks.run_intraday_analysis",
    "market.tasks.fetch_all_market_data",
    "market.tasks.run_full_analysis",
    "market.tasks.append_daily_bars",
    "market.tasks.close_learn_settlement",
    "market.tasks.train_ml_model",
    "market.tasks.assess_ml_reliability",
    "market.tasks.run_end_of_day_pipeline",
    "market.tasks.sync_pe_ratios",
    "market.tasks.sync_holiday_calendar",
    "notifications.tasks.send_daily_digest",
)


@admin_required
@require_POST
def retry_task_view(request, run_id):
    """Re-enqueue the same underlying task a failed TaskRun row recorded —
    Admin-only. Deliberately re-runs by *task name* (looked up against a
    fixed allow-list, not an arbitrary string from the row) rather than
    replaying stored arguments, so this can never be used to re-trigger
    something with attacker-controlled parameters. The retried task uses
    its own normal locking (see market/tasks.py), so clicking retry
    twice, or retrying a task that's already been picked up by the
    schedule again on its own, is Skipped, not duplicated."""
    from market.models import AdminAuditAction, TaskRun, TaskStatus
    from market.services.audit import record_admin_action

    run = get_object_or_404(TaskRun, id=run_id)
    if run.status != TaskStatus.FAILURE:
        messages.error(request, "Only a failed task run can be retried.")
        return redirect("admin_panel")
    if run.task_name not in _RETRYABLE_TASKS:
        messages.error(request, f"{run.task_name} is not retryable from here.")
        return redirect("admin_panel")

    import importlib

    module_name, _, attr = run.task_name.rpartition(".")
    # task names are "market.tasks.foo" / "notifications.tasks.foo" —
    # the Celery task attribute lives on the *tasks* module, not a
    # dotted path all the way down to a class, so this is a plain
    # module.attr lookup, not arbitrary code execution from user input:
    # run.task_name is already constrained to _RETRYABLE_TASKS above.
    try:
        task = getattr(importlib.import_module(module_name), attr)
        task.delay()
        messages.success(request, f"Retry queued for {run.task_name}.")
        record_admin_action(
            request,
            AdminAuditAction.PIPELINE_TRIGGERED,
            {"mode": "retry", "task_name": run.task_name, "original_run_id": run.id},
        )
    except Exception as exc:
        messages.error(request, f"Could not queue retry: {exc}")
    return redirect("admin_panel")


@admin_required
def telegram_report_preview(request):
    """Render today's Telegram ML report without sending it — Admin-only,
    read-only, no Celery involved (cheap enough to run synchronously in
    the request). Uses the exact same rendering + "what changed" logic
    the real send uses, so the preview always matches what would
    actually go out."""
    from notifications.tasks import _compare_with_previous, _report_date_in_configured_tz

    from market.services.ml_daily_report import build_report_context, render_report_sections

    report_date = _report_date_in_configured_tz()
    context = build_report_context(as_of=report_date)
    comparison = _compare_with_previous(report_date, context)
    sections = render_report_sections(context, comparison=comparison)
    return render(request, "market/telegram_report_preview.html", {"sections": sections, "report_date": report_date})


@admin_required
@require_POST
def telegram_report_send(request):
    """Send (or retry, or force-resend) today's Telegram ML daily report —
    Admin-only, POST + CSRF. A plain send/retry respects the
    already-sent-today idempotency guard automatically (see
    notifications.tasks.send_ml_daily_report); force=yes explicitly
    bypasses it to resend a confirmed duplicate, and is audited."""
    from market.models import AdminAuditAction
    from market.services.audit import record_admin_action
    from notifications.tasks import send_ml_daily_report

    force = request.POST.get("force") == "yes"
    send_ml_daily_report.delay(manual=True, force=force)
    if force:
        messages.success(request, "Force resend of today's Telegram ML report queued.")
        record_admin_action(request, AdminAuditAction.TELEGRAM_REPORT_FORCED, {"force": True})
    else:
        messages.success(request, "Telegram ML report send/retry queued.")
    return redirect("admin_panel")


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


def liveness_view(request):
    """Is the process alive enough to route a request? Deliberately
    touches no dependency (DB, broker, disk) — see
    market/services/health.py's module docstring for why."""
    return JsonResponse({"status": "alive"})


def readiness_view(request):
    """Can this process serve real traffic right now? 200 + "ready" only
    if every essential dependency check passes; 503 + "not_ready"
    otherwise, with per-check booleans only — no exception text or
    connection details (those go to the structured log, staff-only)."""
    from market.services.health import readiness_checks

    checks = readiness_checks()
    ready = all(checks.values())
    return JsonResponse({"status": "ready" if ready else "not_ready", "checks": checks}, status=200 if ready else 503)


@login_required
def ticker_json(request):
    """Lightweight, read-only payload for the top market scroll bars —
    authenticated only, like every other market-data view now (see
    accounts/roles.py). Serves cached/DB state only; it must never start
    a sync thread or force a network refresh — that would let any
    visitor trigger a live fetch on every page load. Freshness is
    handled solely by the server-side autosync loop
    (market.services.autosync). Any `refresh`/similar query param is
    intentionally ignored."""
    from django.conf import settings

    from market.services.autosync import get_sync_status
    from market.services.exchange_config import enabled_exchanges
    from market.services.market_hours import both_exchanges_status
    from market.services.rate_limit import is_rate_limited

    limit, period = getattr(settings, "TICKER_JSON_RATE_LIMIT", (60, 60))
    client_ip = request.META.get("REMOTE_ADDR") or "unknown"
    if is_rate_limited(f"ticker_json:{client_ip}", limit, period):
        return JsonResponse({"detail": "Too many requests."}, status=429)

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

    enabled = enabled_exchanges()
    dse = (
        list(Stock.objects.filter(exchange="DSE", is_active=True, last_price__isnull=False).order_by("-last_volume")[:70])
        if "DSE" in enabled
        else []
    )
    cse = (
        list(Stock.objects.filter(exchange="CSE", is_active=True, last_price__isnull=False).order_by("-last_volume")[:70])
        if "CSE" in enabled
        else []
    )

    snapshots = {}
    for snap in MarketSnapshot.objects.filter(exchange__in=enabled).order_by("-as_of"):
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
            "enabled_exchanges": enabled,
        }
    )


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------

PORTFOLIO_DISCLAIMER = (
    "Figures on this page are personal-tracking estimates computed from cached/delayed "
    "market data — not a brokerage statement, tax document, or investment advice."
)


def _owned_portfolio(request, portfolio_id):
    """Every portfolio-scoped view routes through this — a user can only
    ever look up their own portfolio, full stop. get_object_or_404 with
    user=request.user in the filter means a wrong/foreign id 404s exactly
    like a nonexistent one, rather than leaking whether it belongs to
    someone else."""
    return get_object_or_404(Portfolio, id=portfolio_id, user=request.user)


@login_required
def portfolio_redirect(request):
    from market.services.portfolio import get_or_create_default_portfolio

    portfolio = get_or_create_default_portfolio(request.user)
    return redirect("portfolio_detail", portfolio_id=portfolio.id)


@login_required
def portfolio_list(request):
    from market.services.portfolio import get_or_create_default_portfolio, portfolio_summary

    get_or_create_default_portfolio(request.user)
    portfolios = list(Portfolio.objects.filter(user=request.user).order_by("-is_default", "name"))
    summaries = [(p, portfolio_summary(p)) for p in portfolios]
    return render(
        request,
        "market/portfolio_list.html",
        {"summaries": summaries, "create_form": PortfolioForm(user=request.user)},
    )


@login_required
def portfolio_detail(request, portfolio_id):
    from market.services.market_hours import both_exchanges_status
    from market.services.portfolio import portfolio_summary

    portfolio = _owned_portfolio(request, portfolio_id)
    all_portfolios = list(Portfolio.objects.filter(user=request.user).order_by("-is_default", "name"))
    summary = portfolio_summary(portfolio)
    recent_transactions = list(
        portfolio.transactions.select_related("stock").order_by("-transaction_date", "-created_at")[:10]
    )
    return render(
        request,
        "market/portfolio_detail.html",
        {
            "portfolio": portfolio,
            "portfolios": all_portfolios,
            "summary": summary,
            "recent_transactions": recent_transactions,
            "rename_form": PortfolioForm(instance=portfolio, user=request.user),
            "create_form": PortfolioForm(user=request.user),
            "market_hours": both_exchanges_status(),
            "disclaimer": PORTFOLIO_DISCLAIMER,
            "today": timezone.localdate(),
        },
    )


@login_required
@require_POST
def portfolio_create(request):
    form = PortfolioForm(request.POST, user=request.user)
    if form.is_valid():
        p = form.save(commit=False)
        p.user = request.user
        p.is_default = not Portfolio.objects.filter(user=request.user).exists()
        p.save()
        messages.success(request, f'Portfolio "{p.name}" created.')
        return redirect("portfolio_detail", portfolio_id=p.id)
    for field_errors in form.errors.values():
        for error in field_errors:
            messages.error(request, error)
    return redirect("portfolio_list")


@login_required
@require_POST
def portfolio_rename(request, portfolio_id):
    portfolio = _owned_portfolio(request, portfolio_id)
    form = PortfolioForm(request.POST, instance=portfolio, user=request.user)
    if form.is_valid():
        form.save()
        messages.success(request, "Portfolio renamed.")
    else:
        for field_errors in form.errors.values():
            for error in field_errors:
                messages.error(request, error)
    return redirect("portfolio_detail", portfolio_id=portfolio.id)


@login_required
@require_POST
def portfolio_set_default(request, portfolio_id):
    portfolio = _owned_portfolio(request, portfolio_id)
    with transaction.atomic():
        Portfolio.objects.filter(user=request.user, is_default=True).exclude(id=portfolio.id).update(is_default=False)
        portfolio.is_default = True
        portfolio.save(update_fields=["is_default"])
    messages.success(request, f'"{portfolio.name}" is now your default portfolio.')
    return redirect("portfolio_detail", portfolio_id=portfolio.id)


@login_required
@require_POST
def portfolio_delete(request, portfolio_id):
    """Deleting a portfolio cascades to every transaction in it — a real
    financial record — so this requires the user to type the portfolio's
    exact name as a confirmation, not just a click, on top of the
    JS-side confirm() dialog in the template."""
    portfolio = _owned_portfolio(request, portfolio_id)
    if request.POST.get("confirm_name") != portfolio.name:
        messages.error(request, "Portfolio name didn't match — nothing was deleted.")
        return redirect("portfolio_detail", portfolio_id=portfolio.id)
    was_default = portfolio.is_default
    name = portfolio.name
    portfolio.delete()
    if was_default:
        remaining = Portfolio.objects.filter(user=request.user).order_by("id").first()
        if remaining:
            remaining.is_default = True
            remaining.save(update_fields=["is_default"])
    messages.success(request, f'Portfolio "{name}" and its transaction history were deleted.')
    return redirect("portfolio_list")


@login_required
def portfolio_add_transaction(request, portfolio_id):
    from market.services.portfolio import PortfolioValidationError, create_transaction

    portfolio = _owned_portfolio(request, portfolio_id)
    initial = {"transaction_date": timezone.localdate()}
    stock_id = request.GET.get("stock")
    if stock_id:
        initial["stock"] = stock_id
    if request.method == "POST":
        form = TransactionForm(request.POST, portfolio=portfolio)
        if form.is_valid():
            c = form.cleaned_data
            try:
                create_transaction(
                    portfolio, c["stock"], c["transaction_type"], c["quantity"],
                    c["price_per_share"], c["fees"], c["transaction_date"], c["notes"],
                )
                messages.success(request, f'{c["transaction_type"].title()} recorded for {c["stock"].trading_code}.')
                return redirect("portfolio_detail", portfolio_id=portfolio.id)
            except PortfolioValidationError as exc:
                form.add_error(None, str(exc))
    else:
        form = TransactionForm(initial=initial, portfolio=portfolio)
    return render(
        request,
        "market/portfolio_transaction_form.html",
        {"portfolio": portfolio, "form": form, "mode": "add", "title": "Add transaction"},
    )


@login_required
def portfolio_add_holding(request, portfolio_id):
    """Simplified first-touch flow: records a single initial BUY. Reuses
    TransactionForm — the template just hides the transaction_type field
    (fixed to BUY) rather than duplicating validation in a second form."""
    from market.services.portfolio import PortfolioValidationError, create_transaction

    portfolio = _owned_portfolio(request, portfolio_id)
    if request.method == "POST":
        data = request.POST.copy()
        data["transaction_type"] = TransactionType.BUY
        form = TransactionForm(data)
        if form.is_valid():
            c = form.cleaned_data
            try:
                create_transaction(
                    portfolio, c["stock"], TransactionType.BUY, c["quantity"],
                    c["price_per_share"], c["fees"], c["transaction_date"], c["notes"],
                )
                messages.success(request, f'Added {c["stock"].trading_code} to "{portfolio.name}".')
                return redirect("portfolio_detail", portfolio_id=portfolio.id)
            except PortfolioValidationError as exc:
                form.add_error(None, str(exc))
    else:
        form = TransactionForm(initial={"transaction_type": TransactionType.BUY, "transaction_date": timezone.localdate()})
    return render(
        request,
        "market/portfolio_transaction_form.html",
        {"portfolio": portfolio, "form": form, "mode": "add_holding", "title": "Add holding"},
    )


@login_required
def portfolio_edit_transaction(request, portfolio_id, txn_id):
    from market.services.portfolio import PortfolioValidationError, update_transaction

    portfolio = _owned_portfolio(request, portfolio_id)
    txn = get_object_or_404(PortfolioTransaction, id=txn_id, portfolio=portfolio)
    if request.method == "POST":
        form = TransactionForm(request.POST, portfolio=portfolio)
        if form.is_valid():
            c = form.cleaned_data
            try:
                update_transaction(
                    txn, c["transaction_type"], c["quantity"], c["price_per_share"],
                    c["fees"], c["transaction_date"], c["notes"],
                )
                messages.success(request, "Transaction updated.")
                return redirect("portfolio_transactions", portfolio_id=portfolio.id)
            except PortfolioValidationError as exc:
                form.add_error(None, str(exc))
    else:
        form = TransactionForm(
            initial={
                "stock": txn.stock_id,
                "transaction_type": txn.transaction_type,
                "quantity": txn.quantity,
                "price_per_share": txn.price_per_share,
                "fees": txn.fees,
                "transaction_date": txn.transaction_date,
                "notes": txn.notes,
                "allow_fractional": txn.quantity != txn.quantity.to_integral_value(),
            },
            portfolio=portfolio,
        )
    return render(
        request,
        "market/portfolio_transaction_form.html",
        {"portfolio": portfolio, "form": form, "mode": "edit", "txn": txn, "title": "Edit transaction"},
    )


@login_required
@require_POST
def portfolio_delete_transaction(request, portfolio_id, txn_id):
    from market.services.portfolio import PortfolioValidationError, delete_transaction

    portfolio = _owned_portfolio(request, portfolio_id)
    txn = get_object_or_404(PortfolioTransaction, id=txn_id, portfolio=portfolio)
    try:
        delete_transaction(txn)
        messages.success(request, "Transaction deleted.")
    except PortfolioValidationError as exc:
        messages.error(request, f"Could not delete — {exc}")
    return redirect("portfolio_transactions", portfolio_id=portfolio.id)


@login_required
def portfolio_transactions(request, portfolio_id):
    portfolio = _owned_portfolio(request, portfolio_id)
    qs = portfolio.transactions.select_related("stock").order_by("-transaction_date", "-created_at")
    exchange = request.GET.get("exchange", "").strip().upper()
    if exchange:
        qs = qs.filter(stock__exchange=exchange)
    stock_code = request.GET.get("stock", "").strip().upper()
    if stock_code:
        qs = qs.filter(stock__trading_code__icontains=stock_code)
    txn_type = request.GET.get("type", "").strip().upper()
    if txn_type in (TransactionType.BUY, TransactionType.SELL):
        qs = qs.filter(transaction_type=txn_type)
    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "market/portfolio_transactions.html",
        {"portfolio": portfolio, "page_obj": page, "exchange": exchange, "stock_code": stock_code, "txn_type": txn_type},
    )


@login_required
def portfolio_quotes_json(request, portfolio_id):
    """Authenticated, cache/DB-only refresh for an open portfolio page —
    like ticker_json, this must never trigger a live scrape; prices come
    from whatever the background sync pipeline has already written.
    Polled by static/js/portfolio.js."""
    from market.services.market_hours import both_exchanges_status
    from market.services.portfolio import portfolio_summary
    from market.services.rate_limit import is_rate_limited

    portfolio = _owned_portfolio(request, portfolio_id)
    if is_rate_limited(f"portfolio_quotes:{request.user.id}", 30, 60):
        return JsonResponse({"detail": "Too many requests."}, status=429)

    summary = portfolio_summary(portfolio)

    def money(v):
        return str(v) if v is not None else None

    def row_json(r):
        return {
            "exchange": r["exchange"],
            "trading_code": r["trading_code"],
            "quantity": str(r["quantity"]),
            "average_price": money(r["average_price"]),
            "latest_price": money(r["latest_price"]),
            "market_value": money(r["market_value"]),
            "unrealized_pl": money(r["unrealized_pl"]),
            "unrealized_pl_pct": money(r["unrealized_pl_pct"]),
            "today_pl": money(r["today_pl"]),
            "today_pl_pct": money(r["today_pl_pct"]),
            "allocation_pct": money(r["allocation_pct"]),
            "quote_status": r["quote_status"],
            "quote_label": r["quote_label"],
            "quote_as_of": r["quote_as_of"].isoformat() if r["quote_as_of"] else None,
        }

    return JsonResponse(
        {
            "holdings": [row_json(r) for r in summary["holdings"]],
            "total_cost_basis": money(summary["total_cost_basis"]),
            "total_market_value": money(summary["total_market_value"]),
            "total_unrealized_pl": money(summary["total_unrealized_pl"]),
            "total_unrealized_pl_pct": money(summary["total_unrealized_pl_pct"]),
            "today_total_pl": money(summary["today_total_pl"]),
            "market_hours": both_exchanges_status(),
            "generated_at": timezone.now().isoformat(),
        }
    )
