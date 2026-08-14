"""Persistence wrapper around the Phase 5 portfolio backtest engine
(market.services.backtest_engine). Every call creates a NEW BacktestRun
row rather than overwriting a prior one by (name, strategy, exchange) —
each run is an immutable historical record. Legacy rows created by the
pre-Phase-5 engine are left untouched (engine_version="v1", the model
field's default) and are never deleted by this module."""
from __future__ import annotations

from datetime import date, timedelta

from django.utils import timezone

from market.models import BacktestRun
from market.services.backtest_engine import (
    CostConfig,
    PortfolioBacktester,
    PortfolioConfig,
    TRADING_DAYS_PER_YEAR,
    _breakdown_all,
    _buy_and_hold_return_pct,
    _compute_metrics,
    _exchange_index_proxy_return_pct,
)

ENGINE_VERSION = "v3"


def strict_candidate_portfolio_config() -> PortfolioConfig:
    """Pre-registered challenger; do not make this the product default.

    Its parameters were chosen before the final holdout run and are kept in
    one place so evaluation cannot silently tune the holdout.
    """
    return PortfolioConfig(
        strategy="strict_research_v1", position_size_pct=12.0, max_positions=5,
        hold_days=40, target_return_pct=12.0, stop_loss_pct=6.0,
        volume_confirmation_ratio=1.25, min_traded_value=250_000.0,
        universe_size=40, breadth_threshold=0.55,
    )


def run_backtest(
    name: str = "RSI/MACD mean-reversion",
    strategy: str = "rsi_macd_v1",
    exchange: str | None = None,
    hold_days: int = 20,
    start_date: date | None = None,
    end_date: date | None = None,
    cost_config: CostConfig | None = None,
    portfolio_config: PortfolioConfig | None = None,
    include_demo: bool = False,
) -> BacktestRun:
    """include_demo=False (default) excludes synthetic/demo/mirror-fallback
    price rows from the tested universe — a real backtest must not be run
    on fabricated data unless demo mode is explicitly selected."""
    end_date = end_date or timezone.localdate()
    start_date = start_date or (end_date - timedelta(days=365))
    if start_date > end_date:
        raise ValueError("start_date must be <= end_date")

    cost_config = cost_config or CostConfig()
    if portfolio_config is None:
        portfolio_config = strict_candidate_portfolio_config() if strategy == "strict_research_v1" else PortfolioConfig(hold_days=hold_days)

    engine = PortfolioBacktester(
        exchange=exchange,
        start_date=start_date,
        end_date=end_date,
        cost_config=cost_config,
        portfolio_config=portfolio_config,
        include_demo=include_demo,
    )
    result = engine.run()

    if not result.get("ok"):
        run = BacktestRun.objects.create(
            name=name,
            strategy=strategy,
            exchange=exchange or "",
            engine_version=ENGINE_VERSION,
            start_date=start_date,
            end_date=end_date,
            initial_cash=portfolio_config.initial_cash,
            cost_config=cost_config.as_dict(),
            summary={"note": result.get("error", "No trades — insufficient data.")},
        )
        return run

    all_trades = result["trades"]
    clean_trades = [t for t in all_trades if not t["corp_action_flagged"]]
    excluded_trades = [t for t in all_trades if t["corp_action_flagged"]]

    metrics = _compute_metrics(result["equity_curve"], clean_trades, portfolio_config.initial_cash)
    breakdown = _breakdown_all(clean_trades)
    buy_hold = _buy_and_hold_return_pct(result["universe"])
    index_proxy = _exchange_index_proxy_return_pct(exchange, result["data_start_date"], result["data_end_date"])

    warnings = list(result["warnings"])
    if excluded_trades:
        warnings.append(
            f"{len(excluded_trades)} trade(s) excluded from headline metrics: entry or exit landed on a day "
            f"with an implausible single-day return (>=25%), most likely an unadjusted split/rights issue — "
            f"this database has no corporate-action/adjusted-close data source to confirm or correct it."
        )
    for k, v in result["rejections"].items():
        if v:
            warnings.append(f"{v} signal(s) rejected: {k.replace('_', ' ')}")

    run = BacktestRun.objects.create(
        name=name,
        strategy=strategy,
        exchange=exchange or "",
        engine_version=ENGINE_VERSION,
        start_date=start_date,
        end_date=end_date,
        data_start_date=result["data_start_date"],
        data_end_date=result["data_end_date"],
        total_trades=metrics.get("total_trades", 0),
        win_rate=metrics.get("win_rate", 0),
        avg_return_pct=metrics.get("avg_return_pct", 0),
        avg_win_pct=metrics.get("avg_win_pct"),
        avg_loss_pct=metrics.get("avg_loss_pct"),
        max_drawdown_pct=metrics.get("max_drawdown_pct"),
        initial_cash=portfolio_config.initial_cash,
        final_cash=round(result["final_cash"], 2),
        final_equity=metrics.get("final_equity"),
        total_return_pct=metrics.get("total_return_pct"),
        annualized_return_pct=metrics.get("annualized_return_pct"),
        sharpe_ratio=metrics.get("sharpe_ratio"),
        sortino_ratio=metrics.get("sortino_ratio"),
        profit_factor=metrics.get("profit_factor"),
        turnover_pct=metrics.get("turnover_pct"),
        total_costs=metrics.get("total_costs"),
        avg_exposure_pct=metrics.get("avg_exposure_pct"),
        benchmark_buy_hold_return_pct=buy_hold,
        benchmark_index_return_pct=index_proxy,
        cost_config=cost_config.as_dict(),
        breakdown=breakdown,
        warnings=warnings,
        summary={
            "hold_days": portfolio_config.hold_days,
            "target_return_pct": portfolio_config.target_return_pct,
            "position_size_pct": portfolio_config.position_size_pct,
            "max_positions": portfolio_config.max_positions,
            "stocks_tested": result["stocks_tested"],
            "corporate_action_excluded_trades": len(excluded_trades),
            "trading_days_per_year_assumed": TRADING_DAYS_PER_YEAR,
        },
    )
    return run
