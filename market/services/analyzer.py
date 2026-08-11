"""Orchestrates fetch → indicators → patterns → predictions → persistence."""
from __future__ import annotations

import logging

import requests

from django.utils import timezone

from market.models import AnalysisResult, PatternHit, TechnicalSnapshot
from market.services.backtest import run_backtest
from market.services.cse_fetcher import sync_cse_history, sync_cse_live
from market.services.dse_fetcher import seed_demo_universe, sync_dse_history, sync_dse_live
from market.services.indicators import latest_indicator_row, prices_to_df
from market.services.ml_model import blend_score, ml_probability, train_model
from market.services.predictor import predict_stock
from market.models import Stock

logger = logging.getLogger(__name__)


class MarketDataProviderOutage(requests.exceptions.RequestException):
    """Every enabled live provider failed; safe for bounded Celery retry."""


def fetch_all(
    use_demo_if_empty: bool = True,
    include_history: bool = False,
    lookback_days: int | None = None,
) -> dict:
    """
    Fetch market data and persist to SQLite for both DSE and CSE.

    Live quotes always upsert Stock.last_* and today's PriceHistory
    (last successful fetch wins for today). Past history is kept.

    include_history=True also pulls OHLC for both exchanges (merge/upsert).
    """
    dse_live = sync_dse_live()
    cse_live = sync_cse_live()
    dse_hist = None
    cse_hist = None
    if include_history:
        hist_kw = {"use_synthetic_fallback": False}
        if lookback_days is not None:
            hist_kw["lookback_days"] = lookback_days
        dse_hist = sync_dse_history(**hist_kw)
        cse_hist = sync_cse_history(**hist_kw)
    if use_demo_if_empty and Stock.objects.count() < 5:
        demo = seed_demo_universe()
    else:
        demo = None
    live_results = (dse_live, cse_live)
    enabled = [r for r in live_results if not r.get("skipped")]
    succeeded = [r for r in enabled if r.get("ok")]
    # Returning {ok: False} used to be recorded as a Celery SUCCESS, so a
    # provider-wide outage never retried and health data was misleading.
    if enabled and not succeeded:
        errors = "; ".join(str(r.get("error", "provider returned no data")) for r in enabled)
        raise MarketDataProviderOutage(errors[:1000])
    failed = [r for r in enabled if not r.get("ok")]
    attempted = sum(int(r.get("codes", 0) or r.get("count", 0) or 0) for r in live_results)
    successful = sum(int(r.get("count", 0) or r.get("ok", 0) or 0) for r in live_results if r.get("ok"))
    return {
        "dse_live": dse_live, "cse_live": cse_live, "dse_hist": dse_hist, "cse_hist": cse_hist, "demo": demo,
        "partial": bool(failed), "symbols_attempted": attempted,
        "symbols_successful": successful, "symbols_failed": len(failed),
        "failed_symbols": [{"provider": "DSE" if r is dse_live else "CSE", "error": r.get("error", "unknown")} for r in failed],
    }


def analyze_stock(stock: Stock, use_ml: bool = True, include_demo: bool = False) -> AnalysisResult:
    """include_demo=False (default) excludes synthetic/demo/mirror-fallback
    price rows — real analysis output must not be built on fabricated data
    unless demo mode is explicitly selected."""
    prices_qs = stock.prices.all() if include_demo else stock.prices.live()
    df = prices_to_df(prices_qs)
    as_of = timezone.localdate()
    if not df.empty:
        as_of = df.iloc[-1]["date"].date() if hasattr(df.iloc[-1]["date"], "date") else df.iloc[-1]["date"]

    # Technical snapshot
    ind = latest_indicator_row(df)
    if ind:
        TechnicalSnapshot.objects.update_or_create(
            stock=stock,
            as_of=as_of,
            defaults={
                "sma_20": ind.get("sma_20"),
                "sma_50": ind.get("sma_50"),
                "sma_200": ind.get("sma_200"),
                "ema_12": ind.get("ema_12"),
                "ema_26": ind.get("ema_26"),
                "rsi_14": ind.get("rsi_14"),
                "macd": ind.get("macd"),
                "macd_signal": ind.get("macd_signal"),
                "macd_hist": ind.get("macd_hist"),
                "bb_upper": ind.get("bb_upper"),
                "bb_middle": ind.get("bb_middle"),
                "bb_lower": ind.get("bb_lower"),
                "atr_14": ind.get("atr_14"),
                "volume_sma_20": ind.get("volume_sma_20"),
                "support": ind.get("support"),
                "resistance": ind.get("resistance"),
            },
        )

    pred = predict_stock(df, group=stock.group, pe_ratio=stock.pe_ratio)

    # Patterns
    PatternHit.objects.filter(stock=stock, as_of=as_of).delete()
    for p in pred.patterns:
        PatternHit.objects.create(
            stock=stock,
            as_of=as_of,
            name=p["name"],
            direction=p["direction"],
            strength=p["strength"],
            description=p["description"],
        )

    ml_score = None
    score = pred.score
    if use_ml and len(df) >= 80:
        ml_prob = ml_probability(df, exchange=stock.exchange)
        if ml_prob is not None:
            ml_score = round(ml_prob * 100, 2)
            score = blend_score(pred.score, ml_prob)
            # Re-derive action lightly from blended score
            from market.services.predictor import action_from_score

            pred.action = action_from_score(score)
            pred.score = round(score, 2)
            # is_safe_buy was computed from the pre-blend rule score; re-apply the
            # same score/confidence thresholds so a blend that pulls the score
            # below the safe-buy bar (even while action stays BUY) clears the flag.
            if pred.action != "BUY" or pred.score < 28 or pred.confidence < 0.4:
                pred.is_safe_buy = False

    result, _ = AnalysisResult.objects.update_or_create(
        stock=stock,
        as_of=as_of,
        defaults={
            "action": pred.action,
            "score": pred.score,
            "confidence": pred.confidence,
            "risk_level": pred.risk_level,
            "is_safe_buy": pred.is_safe_buy,
            "maturity_days_est": pred.maturity_days_est,
            "peak_days_est": pred.peak_days_est,
            "expected_return_pct": pred.expected_return_pct,
            "probability": pred.probability,
            "rationale": pred.rationale,
            "features": pred.features,
            "ml_score": ml_score,
        },
    )
    return result


def run_full_analysis(train_ml: bool = True, include_demo: bool = False) -> dict:
    from market.models import Exchange
    from market.services.exchange_config import enabled_exchanges

    enabled = enabled_exchanges()

    if train_ml:
        ml_info = train_model()
    else:
        ml_info = {"ok": False, "skipped": True}

    analyzed = 0
    errors = 0
    # Only currently-enabled exchanges get new AnalysisResult rows — a
    # disabled exchange's existing rows are simply not refreshed, same
    # treatment as its price data.
    for stock in Stock.objects.filter(is_active=True, exchange__in=enabled):
        try:
            analyze_stock(stock, use_ml=True, include_demo=include_demo)
            analyzed += 1
        except Exception as exc:
            logger.exception("Analyze failed for %s: %s", stock, exc)
            errors += 1

    backtests = {}
    if Exchange.DSE in enabled:
        backtests["DSE"] = run_backtest(name="DSE RSI/MACD 1Y", exchange="DSE").id
    if Exchange.CSE in enabled:
        backtests["CSE"] = run_backtest(name="CSE RSI/MACD 1Y", exchange="CSE").id
    return {
        "analyzed": analyzed,
        "errors": errors,
        "ml": ml_info,
        "backtests": backtests,
        "enabled_exchanges": enabled,
    }
