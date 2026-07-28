"""Backtesting utilities for rule-based setups on 1y history."""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
from django.utils import timezone

from market.models import BacktestRun, Exchange, Stock
from market.services.indicators import compute_indicators, prices_to_df
from market.services.predictor import estimate_maturity_and_peak


def _simulate_stock(df: pd.DataFrame, hold_days: int = 20, target: float = 0.05) -> list[dict]:
    data = compute_indicators(df)
    trades = []
    i = 40
    while i < len(data) - hold_days - 1:
        row = data.iloc[i]
        rsi = row.get("rsi_14")
        macd_up = (
            pd.notna(row.get("macd"))
            and data.iloc[i - 1]["macd"] <= data.iloc[i - 1]["macd_signal"]
            and row["macd"] > row["macd_signal"]
        )
        if not ((pd.notna(rsi) and rsi < 35) or macd_up):
            i += 1
            continue
        entry = float(row["close"])
        if entry <= 0:
            i += 1
            continue
        window = data.iloc[i + 1 : i + hold_days + 1]
        exit_price = float(window.iloc[-1]["close"])
        # Optional early exit at target
        for _, frow in window.iterrows():
            if frow["close"] >= entry * (1 + target):
                exit_price = float(frow["close"])
                break
        if exit_price <= 0:
            i += hold_days
            continue
        ret = (exit_price / entry - 1) * 100
        peak = float(window["close"].max())
        peak_ret = (peak / entry - 1) * 100 if entry else 0
        trough = float(window["close"].min())
        dd = (trough / entry - 1) * 100 if entry else 0
        trades.append({"return_pct": ret, "peak_return_pct": peak_ret, "drawdown_pct": dd})
        i += hold_days  # non-overlapping
    return trades


def run_backtest(
    name: str = "RSI/MACD mean-reversion",
    strategy: str = "rsi_macd_v1",
    exchange: str | None = None,
    hold_days: int = 20,
) -> BacktestRun:
    qs = Stock.objects.filter(is_active=True)
    if exchange:
        qs = qs.filter(exchange=exchange)

    all_trades: list[dict] = []
    maturity_days: list[int] = []
    peak_days: list[int] = []

    for stock in qs:
        df = prices_to_df(stock.prices.all())
        if len(df) < 100:
            continue
        all_trades.extend(_simulate_stock(df, hold_days=hold_days))
        est = estimate_maturity_and_peak(df)
        if est["maturity_days"]:
            maturity_days.append(est["maturity_days"])
        if est["peak_days"]:
            peak_days.append(est["peak_days"])

    if not all_trades:
        end = timezone.localdate()
        run, _ = BacktestRun.objects.update_or_create(
            name=name,
            strategy=strategy,
            exchange=exchange or "",
            defaults={
                "start_date": end - timedelta(days=365),
                "end_date": end,
                "total_trades": 0,
                "win_rate": 0,
                "avg_return_pct": 0,
                "avg_days_to_target": None,
                "avg_days_to_peak": None,
                "max_drawdown_pct": None,
                "summary": {"note": "No trades — need more price history."},
            },
        )
        return run

    returns = np.array([t["return_pct"] for t in all_trades])
    wins = float((returns > 0).mean())
    avg_ret = float(returns.mean())
    max_dd = float(np.min([t["drawdown_pct"] for t in all_trades]))
    end = timezone.localdate()
    run, _ = BacktestRun.objects.update_or_create(
        name=name,
        strategy=strategy,
        exchange=exchange or "",
        defaults={
            "start_date": end - timedelta(days=365),
            "end_date": end,
            "total_trades": len(all_trades),
            "win_rate": round(wins, 4),
            "avg_return_pct": round(avg_ret, 3),
            "avg_days_to_target": float(np.mean(maturity_days)) if maturity_days else None,
            "avg_days_to_peak": float(np.mean(peak_days)) if peak_days else None,
            "max_drawdown_pct": round(max_dd, 3),
            "summary": {
                "hold_days": hold_days,
                "median_return_pct": float(np.median(returns)),
                "p75_return_pct": float(np.percentile(returns, 75)),
                "stocks_tested": qs.count(),
            },
        },
    )
    return run
