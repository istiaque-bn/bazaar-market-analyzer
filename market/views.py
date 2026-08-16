from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from datetime import datetime

from accounts.decorators import admin_required, staff_or_admin_required
from market.forms import PortfolioCSVImportForm, PortfolioForm, PortfolioGoalForm, ResearchNoteForm, TransactionForm
from market.forms import AdminReminderForm
from market.models import (
    AnalysisResult,
    BacktestRun,
    MarketSnapshot,
    PatternHit,
    Portfolio,
    PortfolioGoal,
    PortfolioTransaction,
    ResearchNote,
    Stock,
    TechnicalSnapshot,
    TransactionType,
    Watchlist,
)
from market.services.indicators import prices_to_df
from market.services.predictor import CONFIDENCE_SCALE, RESEARCH_DISCLAIMER, predict_price_at_date
from market.services.signal_status import signal_status
from market.views_dashboard import _dashboard_health_issue, dashboard, home  # noqa: F401
from market.portfolio_access import owned_portfolio as _owned_portfolio
from notifications.forms import AlertRuleForm
from notifications.models import Alert, AlertRule


LEGAL_PAGES = {
    "privacy": {"title": "Privacy Policy", "intro": "This policy explains how Stock Monitoring operates Stock Bazaar and handles personal information.", "sections": [("Information we collect", "We collect account details you provide, including username, email address and authentication information. When you use the product, we also store the portfolios, transactions, watchlists, notes, feedback, notification preferences and settings you choose to create."), ("Why we use it", "We use this information to authenticate your account, provide portfolio and watchlist features, deliver requested notifications, improve reliability, investigate misuse and meet legal or security obligations."), ("Sharing and retention", "We do not sell personal information. Access is limited to authorised operators and service providers needed to run the product. We retain information only for as long as reasonably necessary for the service, security, dispute handling and legal obligations; backups may retain encrypted copies for a limited rotation period."), ("Your choices", "You may request access, correction or deletion of your personal information by emailing stockbazex@gmail.com. We may need to retain limited information where required for security, fraud prevention or law."), ("Security", "We use access controls, encrypted off-site backups and reasonable operational safeguards. No online service can guarantee absolute security, so please use a unique password and notify us promptly if you suspect unauthorised access.")]},
    "terms": {"title": "Terms of Service", "intro": "By using Stock Bazaar, you agree to these terms.", "sections": [("What the service is", "Stock Bazaar is an educational market-analysis and stock-monitoring product operated by Stock Monitoring. It is not a broker, dealer, exchange member, custodian, portfolio manager or investment adviser. It does not accept orders, execute trades, hold customer money or securities, or provide personalised investment advice."), ("Your decisions", "You are solely responsible for evaluating information and for every investment decision, order and tax or regulatory obligation. Do not treat a signal, forecast, backtest, portfolio view or notification as a recommendation to buy, sell or hold a security."), ("Acceptable use", "Use the service lawfully and do not interfere with it, attempt unauthorised access, scrape or redistribute data where prohibited, or use the product to mislead others. You are responsible for maintaining the confidentiality of your account credentials."), ("Availability and changes", "We may change, suspend or discontinue features to maintain security, data quality or legal compliance. Market data, models and notifications can be delayed, incomplete or unavailable; the product is provided on an as-available basis."), ("Liability", "To the maximum extent permitted by law, Stock Monitoring is not liable for investment losses, missed opportunities, data errors, service interruptions or indirect losses arising from use of Stock Bazaar. Nothing in these terms removes rights that cannot legally be excluded."), ("Contact", "For questions about these terms, contact stockbazex@gmail.com.")]},
    "risk": {"title": "Risk Disclosure", "intro": "Investing in capital markets involves substantial risk and may result in partial or total loss of capital.", "sections": [("Market risk", "Security prices can move quickly because of company events, market conditions, liquidity, regulation, macroeconomic changes and factors that cannot be predicted. You may not be able to sell at a desired price or at all."), ("Information and data risk", "Displayed prices, corporate information and indicators may be delayed, incomplete, corrected or unavailable. You must independently verify information with appropriate official and licensed sources before acting."), ("Model and backtest risk", "Forecasts, signals, technical indicators, historical analogues, machine-learning outputs and backtests are uncertain. They can be wrong, degrade over time or perform differently in live conditions. Historical and simulated results do not guarantee future results."), ("No suitability assessment", "Stock Bazaar does not know your financial situation, objectives, experience or risk tolerance and does not assess suitability. Consider independent professional advice where appropriate."), ("Action required", "Only invest money you can afford to lose, diversify where suitable for your circumstances, understand transaction costs and comply with applicable rules. Do not make a decision solely because of this product.")]},
    "conflicts": {"title": "Conflict-of-Interest Disclosure", "intro": "Stock Bazaar does not execute trades or hold customer securities.", "sections": [("Potential conflicts", "Operators, contributors or affiliates could hold, trade or have another financial interest in securities or companies discussed by the service. They could also have commercial relationships with third parties."), ("Our approach", "A material known conflict connected to published coverage should be disclosed clearly before or with that coverage. Product and model changes are intended to be governed by data quality and reliability evidence rather than trading outcomes."), ("Your responsibility", "You should treat all information as educational research, independently assess potential conflicts and make your own decisions. Contact stockbazex@gmail.com to report a potential undisclosed conflict.")]},
    "methodology": {"title": "Model Methodology", "intro": "Stock Bazaar provides research estimates and screening information, not recommendations or personalised advice.", "sections": [("Inputs", "The product processes available historical market data, technical indicators, price and volume patterns, historical analogues and user-entered portfolio information. Source data can be delayed or corrected and is not guaranteed complete."), ("Outputs", "Screening labels and model outputs describe calculated patterns or estimates at a point in time. They do not predict outcomes with certainty and should not be interpreted as trade instructions."), ("Validation and monitoring", "Where models are used, Stock Bazaar compares them with simpler or naive baselines and monitors settled outcomes. A model may be marked experimental, limited or inactive when evidence is weak or operational checks fail."), ("Limitations", "Models can overfit historical data, fail during changing market regimes, inherit data errors and lose effectiveness after deployment. Backtests do not include every real-world constraint and cannot establish future performance."), ("Governance", "Methodology, thresholds and available features may change as data quality, reliability testing or legal requirements change. Material limitations are intended to be shown in the product where relevant.")]},
    "refunds": {"title": "Refund Policy", "intro": "No paid plan or payment collection is currently active.", "sections": [("Current position", "Because Stock Bazaar does not currently accept payments, there is no refund period and refunds are not applicable."), ("Future paid services", "Before any paid feature is offered, its price, billing cycle, cancellation method, eligibility and refund terms will be shown before payment is accepted.")]},
    "data": {"title": "Market Data and Licensing Notice", "intro": "Stock Bazaar is independent of DSE, CSE and BSEC.", "sections": [("Data limitations", "Market data may be delayed, incomplete, corrected or subject to source restrictions. It must not be relied upon as an official market record."), ("Licensing", "Before any commercial redistribution, real-time data claim or use beyond applicable source terms, Stock Bazaar must obtain the necessary written permission or licence from the relevant exchange or data provider."), ("Regulatory position", "Stock Bazaar provides educational analysis only. It does not provide brokerage, order execution, custody or personalised investment advice. Its operation and disclosures require independent legal and regulatory review before launch or material expansion.")]},
}


def legal_page(request, slug):
    page = LEGAL_PAGES.get(slug)
    if page is None:
        raise Http404("Policy page not found.")
    return render(request, "market/legal.html", {"title": page["title"], "intro": page["intro"], "sections": page["sections"]})


def product_documentation(request):
    return render(request, "market/product_documentation.html")


@admin_required
def paper_trading_view(request):
    from django.db.models import Avg, Count, Q
    from market.models import PaperLearningFeedback
    from market.services.paper_learning import paper_evidence_report
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
            "learning_report": paper_evidence_report(account),
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


@admin_required
def admin_reminders_view(request):
    from notifications.models import AdminReminder
    form = AdminReminderForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        AdminReminder.objects.create(admin=request.user, **form.cleaned_data)
        messages.success(request, "Reminder saved. You will receive it on the selected date.")
        return redirect("admin_reminders")
    return render(request, "market/admin_reminders.html", {"form": form, "reminders": AdminReminder.objects.filter(admin=request.user)})


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
                "open": round(float(r["open"]), 2), "high": round(float(r["high"]), 2), "low": round(float(r["low"]), 2),
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
    from market.services.close_learn import compute_beta, learn_status
    from market.services.price_format import round_to_tick
    from market.services.data_quality import stock_provenance_summary

    # Computed fresh from the same df as the price chart (not read from
    # TechnicalSnapshot.beta_90d) so the displayed number and its scatter
    # points are always mutually consistent, even if it's been a while
    # since the last analysis run touched this stock.
    beta_90d, beta_pairs = compute_beta(df, exchange=stock.exchange) if not df.empty else (None, [])

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
    drivers = []
    if analysis:
        drivers.extend([
            {"label": "Model score", "value": f"{analysis.score:.0f}/100", "tone": "up" if analysis.score >= 60 else "down" if analysis.score <= 40 else ""},
            {"label": "Expected return", "value": f"{analysis.expected_return_pct:+.2f}%" if analysis.expected_return_pct is not None else "Not available", "tone": "up" if (analysis.expected_return_pct or 0) > 0 else "down" if (analysis.expected_return_pct or 0) < 0 else ""},
            {"label": "Prediction confidence", "value": f"{analysis.confidence * 100:.0f}%" if analysis.confidence is not None else "Not available", "tone": ""},
        ])
    if tech:
        drivers.extend([
            {"label": "RSI (14)", "value": f"{tech.rsi_14:.1f}" if tech.rsi_14 is not None else "Not available", "tone": ""},
            {"label": "MACD histogram", "value": f"{tech.macd_hist:.3f}" if tech.macd_hist is not None else "Not available", "tone": "up" if (tech.macd_hist or 0) > 0 else "down" if (tech.macd_hist or 0) < 0 else ""},
        ])
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
            "beta_90d": beta_90d,
            "beta_pairs": beta_pairs,
            "prediction_drivers": drivers,
            "provenance": stock_provenance_summary(stock),
            "research_notes": ResearchNote.objects.filter(user=request.user, stock=stock) if request.user.is_authenticated else [],
            "research_note_form": ResearchNoteForm(),
        },
    )


def market_events(request):
    """Public, source-linked events supplied by staff pending an official feed."""
    from market.models import MarketEvent

    events = MarketEvent.objects.filter(is_public=True, event_date__gte=timezone.localdate()).select_related("stock")[:100]
    return render(request, "market/market_events.html", {"events": events})


def public_market_overview(request):
    from market.services.exchange_config import enabled_exchanges

    snapshots = MarketSnapshot.objects.filter(exchange__in=enabled_exchanges()).order_by("exchange", "-as_of")
    latest = {}
    for snapshot in snapshots:
        latest.setdefault(snapshot.exchange, snapshot)
    return render(request, "market/public_overview.html", {"snapshots": latest.values()})


def fundamentals_view(request, exchange, code):
    stock = _get_stock_for_public_route(exchange, code)
    rows = stock.fundamentals.all()
    sector_pes = Stock.objects.filter(sector=stock.sector, pe_ratio__gt=0).values_list("pe_ratio", flat=True)
    values = list(sector_pes)
    return render(request, "market/fundamentals.html", {"stock": stock, "rows": rows, "sector_pe": round(sum(values)/len(values), 2) if values else None})


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
def compare_stocks(request):
    """Compare up to four saved listings without mixing in fresh/live data."""
    raw_ids = [item for item in request.GET.getlist("stocks") if item.isdigit()][:4]
    available = Stock.objects.filter(is_active=True).order_by("trading_code")
    selected = list(available.filter(id__in=raw_ids))
    analyses, techs = {}, {}
    for analysis in AnalysisResult.objects.filter(stock__in=selected).order_by("stock_id", "-as_of"):
        analyses.setdefault(analysis.stock_id, analysis)
    for tech in TechnicalSnapshot.objects.filter(stock__in=selected).order_by("stock_id", "-as_of"):
        techs.setdefault(tech.stock_id, tech)
    rows = [{"stock": stock, "analysis": analyses.get(stock.id), "tech": techs.get(stock.id)} for stock in selected]
    return render(request, "market/compare_stocks.html", {"available_stocks": available, "rows": rows, "selected_ids": [stock.id for stock in selected]})


@login_required
def sector_heatmap(request):
    latest = {}
    for analysis in AnalysisResult.objects.select_related("stock").filter(stock__is_active=True).order_by("stock_id", "-as_of"):
        latest.setdefault(analysis.stock_id, analysis)
    buckets = {}
    for analysis in latest.values():
        sector = analysis.stock.sector or "Unclassified"
        row = buckets.setdefault(sector, {"sector": sector, "stocks": 0, "buy": 0, "sell": 0, "score_total": 0.0, "change_total": 0.0, "change_count": 0})
        row["stocks"] += 1
        row["buy"] += analysis.action == "BUY"
        row["sell"] += analysis.action == "SELL"
        row["score_total"] += analysis.score or 0
        if analysis.stock.last_change_pct is not None:
            row["change_total"] += analysis.stock.last_change_pct
            row["change_count"] += 1
    sectors = []
    for row in buckets.values():
        row["avg_score"] = round(row.pop("score_total") / row["stocks"], 1)
        row["avg_change"] = round(row.pop("change_total") / row.pop("change_count"), 2) if row["change_count"] else None
        row["heat"] = max(0, min(100, round((row["avg_score"] + 100) / 2)))
        sectors.append(row)
    return render(request, "market/sector_heatmap.html", {"sectors": sorted(sectors, key=lambda item: item["avg_score"], reverse=True)})


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
    return render(request, "market/alerts.html", {"alerts": alerts, "rules": AlertRule.objects.filter(user=request.user), "rule_form": AlertRuleForm()})


@login_required
@require_POST
def alert_rule_create(request):
    form = AlertRuleForm(request.POST)
    if form.is_valid():
        rule = form.save(commit=False)
        rule.user = request.user
        rule.save()
        messages.success(request, "Alert rule saved. Telegram delivery also requires Telegram alerts enabled in your profile.")
    else:
        messages.error(request, "Please correct the alert rule fields.")
    return redirect("alerts")


@login_required
@require_POST
def alert_rule_toggle(request, rule_id):
    rule = get_object_or_404(AlertRule, id=rule_id, user=request.user)
    rule.is_active = not rule.is_active
    rule.save(update_fields=["is_active", "updated_at"])
    messages.success(request, f"Alert rule {'enabled' if rule.is_active else 'paused'}.")
    return redirect("alerts")


@login_required
@require_POST
def alert_rule_delete(request, rule_id):
    rule = get_object_or_404(AlertRule, id=rule_id, user=request.user)
    rule.delete()
    messages.success(request, "Alert rule deleted.")
    return redirect("alerts")


@login_required
@require_POST
def research_note_create(request, exchange, code):
    stock = _get_stock_for_public_route(exchange, code)
    form = ResearchNoteForm(request.POST)
    if form.is_valid():
        note = form.save(commit=False)
        note.user = request.user
        note.stock = stock
        note.save()
        messages.success(request, "Private research note saved.")
    else:
        messages.error(request, "Please add a title and note body.")
    return redirect("stock_detail", exchange=stock.exchange, code=stock.trading_code)


@login_required
@require_POST
def research_note_delete(request, note_id):
    note = get_object_or_404(ResearchNote, id=note_id, user=request.user)
    stock = note.stock
    note.delete()
    messages.success(request, "Research note deleted.")
    return redirect("stock_detail", exchange=stock.exchange, code=stock.trading_code)


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
    from market.services.next_close_diagnostics import next_close_diagnostics

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
            "next_close_diagnostics": next_close_diagnostics(),
        },
    )


@admin_required
def shadow_model_monitor(request):
    from market.models import ShadowForecast, TaskRun
    from market.services.shadow_model import shadow_report
    rows = ShadowForecast.objects.select_related("stock").order_by("-target_date", "stock__trading_code")[:100]
    tasks = TaskRun.objects.filter(task_name__in=["market.tasks.run_shadow_model", "notifications.tasks.send_shadow_model_report"]).order_by("-started_at")[:20]
    return render(request, "market/shadow_model_monitor.html", {"report": shadow_report(), "forecasts": rows, "tasks": tasks})


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
            # The task queues bounded historical batches and appends analysis
            # only after they complete; an outer chain would run analysis
            # immediately after this short dispatcher returns.
            fetch_all_market_data.delay(include_history=True)
            messages.success(request, "Live fetch and bounded history batches, then analysis, queued.")
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
    goal, _ = PortfolioGoal.objects.get_or_create(portfolio=portfolio)
    allocations = [r["allocation_pct"] for r in summary["holdings"] if r["allocation_pct"] is not None]
    largest_position = max(allocations) if allocations else 0
    risk = {
        "largest_position": largest_position,
        "limit": goal.max_single_position_pct,
        "is_concentrated": largest_position > goal.max_single_position_pct,
        "goal_progress": round(float(summary.total_market_value / goal.target_value * 100), 1)
        if goal.target_value and goal.target_value > 0 and summary["total_market_value"] is not None else None,
    }
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
            "goal": goal,
            "goal_form": PortfolioGoalForm(instance=goal),
            "risk": risk,
            "market_hours": both_exchanges_status(),
            "disclaimer": PORTFOLIO_DISCLAIMER,
            "today": timezone.localdate(),
            "csv_import_form": PortfolioCSVImportForm(),
        },
    )


@login_required
@require_POST
def portfolio_goal_save(request, portfolio_id):
    portfolio = _owned_portfolio(request, portfolio_id)
    goal, _ = PortfolioGoal.objects.get_or_create(portfolio=portfolio)
    form = PortfolioGoalForm(request.POST, instance=goal)
    if form.is_valid():
        form.save()
        messages.success(request, "Portfolio goal and concentration limit saved.")
    else:
        messages.error(request, "Please correct the portfolio goal fields.")
    return redirect("portfolio_detail", portfolio_id=portfolio.id)


@login_required
def portfolio_export_csv(request, portfolio_id):
    import csv

    from market.services.portfolio import portfolio_summary

    portfolio = _owned_portfolio(request, portfolio_id)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{portfolio.name.lower().replace(" ", "-")}-holdings.csv"'
    writer = csv.writer(response)
    writer.writerow(["Code", "Exchange", "Quantity", "Average buy", "Current price", "Market value", "Gain/loss", "Allocation %"])
    for row in portfolio_summary(portfolio)["holdings"]:
        writer.writerow([row["trading_code"], row["exchange"], row["quantity"], row["average_price"], row["latest_price"], row["market_value"], row["unrealized_pl"], row["allocation_pct"]])
    return response


@login_required
@require_POST
def portfolio_import_csv(request, portfolio_id):
    """Import broker-exported BUY/SELL rows atomically.

    Accepted headings (case-insensitive): code/trading_code, exchange,
    quantity, price/price_per_share/avg_buy, transaction_date/date, fees,
    type/transaction_type, notes, thesis, target_price, invalidation and
    post_trade_review.  A rejected row imports nothing, so the user's ledger
    cannot be left half-updated by a malformed broker export.
    """
    import csv
    import io
    from decimal import Decimal, InvalidOperation

    from market.services.portfolio import PortfolioValidationError, create_transaction, validate_ledger_after_mutation

    portfolio = _owned_portfolio(request, portfolio_id)
    form = PortfolioCSVImportForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, next(iter(form.errors.get("csv_file", ["Please choose a CSV file."]))))
        return redirect("portfolio_detail", portfolio_id=portfolio.id)
    try:
        raw = form.cleaned_data["csv_file"].read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(raw))
        if not reader.fieldnames:
            raise ValueError("CSV needs a header row.")
        rows = [{(key or "").strip().lower(): (value or "").strip() for key, value in row.items()} for row in reader]
        if not rows:
            raise ValueError("CSV contains no transaction rows.")

        def value(row, *names, default=""):
            return next((row[name] for name in names if row.get(name, "") != ""), default)

        prepared = []
        for line, row in enumerate(rows, start=2):
            code = value(row, "code", "trading_code", "symbol").upper()
            exchange = value(row, "exchange", default="DSE").upper()
            if not code:
                raise ValueError(f"Row {line}: code is required.")
            if exchange not in ("DSE", "CSE"):
                raise ValueError(f"Row {line}: exchange must be DSE or CSE.")
            stock = Stock.objects.filter(exchange=exchange, trading_code__iexact=code, is_active=True).first()
            if not stock:
                raise ValueError(f"Row {line}: {code} ({exchange}) is not an available stock.")
            try:
                quantity = Decimal(value(row, "quantity", "qty"))
                price = Decimal(value(row, "price_per_share", "price", "avg_buy", "average_price"))
                fees = Decimal(value(row, "fees", "fee", "charges", default="0"))
                target_raw = value(row, "target_price", "target")
                target = Decimal(target_raw) if target_raw else None
            except InvalidOperation:
                raise ValueError(f"Row {line}: quantity, price, fees, and target must be valid numbers.")
            if quantity <= 0 or price < 0 or fees < 0 or (target is not None and target < 0):
                raise ValueError(f"Row {line}: quantity must be positive; price, fees, and target cannot be negative.")
            date_raw = value(row, "transaction_date", "date")
            try:
                transaction_date = datetime.strptime(date_raw, "%Y-%m-%d").date() if date_raw else timezone.localdate()
            except ValueError:
                raise ValueError(f"Row {line}: date must use YYYY-MM-DD.")
            txn_type = value(row, "transaction_type", "type", "side", default="BUY").upper()
            if txn_type not in (TransactionType.BUY, TransactionType.SELL):
                raise ValueError(f"Row {line}: type must be BUY or SELL.")
            if quantity != quantity.to_integral_value():
                raise ValueError(f"Row {line}: DSE/CSE shares must be a whole number.")
            prepared.append((transaction_date, line, stock, txn_type, quantity, price, fees, row, target))

        with transaction.atomic():
            changed_stocks = set()
            for transaction_date, _line, stock, txn_type, quantity, price, fees, row, target in sorted(prepared, key=lambda item: (item[0], item[1])):
                create_transaction(
                    portfolio, stock, txn_type, quantity, price, fees, transaction_date,
                    value(row, "notes", "note"), thesis=value(row, "thesis"), target_price=target,
                    invalidation=value(row, "invalidation", "risk", "risk_invalidation"),
                    post_trade_review=value(row, "post_trade_review", "review"),
                )
                changed_stocks.add(stock)
            for stock in changed_stocks:
                validate_ledger_after_mutation(portfolio, stock)
    except (UnicodeDecodeError, ValueError, PortfolioValidationError) as exc:
        messages.error(request, f"Import cancelled: {exc}")
        return redirect("portfolio_detail", portfolio_id=portfolio.id)
    messages.success(request, f"Imported {len(prepared)} transaction{'s' if len(prepared) != 1 else ''} from CSV.")
    return redirect("portfolio_detail", portfolio_id=portfolio.id)


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
                    thesis=c["thesis"], target_price=c["target_price"], invalidation=c["invalidation"], post_trade_review=c["post_trade_review"],
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
                    thesis=c["thesis"], target_price=c["target_price"], invalidation=c["invalidation"], post_trade_review=c["post_trade_review"],
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
                    thesis=c["thesis"], target_price=c["target_price"], invalidation=c["invalidation"], post_trade_review=c["post_trade_review"],
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
                "thesis": txn.thesis,
                "target_price": txn.target_price,
                "invalidation": txn.invalidation,
                "post_trade_review": txn.post_trade_review,
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
