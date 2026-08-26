"""Read-only diagnostics for immutable next-close forecasts.

This is an evaluation surface, not a serving policy.  It compares the
currently-served all-forecast policy with plausible abstention/label policies
using only outcomes that were settled after an immutable forecast existed.
"""
from __future__ import annotations

from collections import defaultdict
import numpy as np

from market.models import Exchange, PredictionSnapshot

MIN_DIAGNOSTIC_SAMPLE = 20
MIN_POLICY_SAMPLE = 30

# These policies are diagnostic candidates only.  A policy has to beat the
# current all-forecast behaviour on settled data before it can be considered
# for a later, explicit serving change.
RETURN_THRESHOLDS = (0.0, 0.003, 0.005, 0.01)
CONFIDENCE_THRESHOLDS = (0.0, 0.55, 0.60, 0.65, 0.70)


def _confidence_bucket(value):
    value = float(value or 0)
    if value < .55:
        return "Below 55%"
    if value < .60:
        return "55–59%"
    if value < .65:
        return "60–64%"
    if value < .70:
        return "65–69%"
    if value < .80:
        return "70–79%"
    return "80%+"


def _metrics(rows):
    if not rows:
        return {"count": 0, "mape": None, "direction_hit_rate": None, "win_rate": None}
    mape = [abs((r.predicted_price - r.outcome_price) / r.outcome_price) * 100 for r in rows if r.predicted_price and r.outcome_price]
    directional = [r for r in rows if r.predicted_return is not None and r.outcome_return is not None]
    hits = [((r.predicted_return >= 0) == (r.outcome_return >= 0)) for r in directional]
    return {"count": len(rows), "mape": round(float(np.mean(mape)), 3) if mape else None, "direction_hit_rate": round(float(np.mean(hits))*100, 1) if hits else None, "win_rate": round(float(np.mean([r.outcome_return > 0 for r in directional]))*100, 1) if directional else None}


def _error_metrics(rows):
    """Error/skill values that remain meaningful when forecast coverage drops."""
    usable = [r for r in rows if r.predicted_return is not None and r.outcome_return is not None]
    if not usable:
        return {"count": 0, "mae_return": None, "naive_mae_return": None, "skill_vs_naive": None, "direction_hit_rate": None}
    errors = np.asarray([abs(float(r.predicted_return) - float(r.outcome_return)) for r in usable])
    naive = np.asarray([abs(float(r.outcome_return)) for r in usable])
    hits = np.asarray([(float(r.predicted_return) >= 0) == (float(r.outcome_return) >= 0) for r in usable])
    naive_mae = float(naive.mean())
    return {
        "count": len(usable),
        "mae_return": round(float(errors.mean()), 6),
        "naive_mae_return": round(naive_mae, 6),
        "skill_vs_naive": round(1 - float(errors.mean()) / naive_mae, 4) if naive_mae > 1e-12 else None,
        "direction_hit_rate": round(float(hits.mean()) * 100, 1),
    }


def _status_for(metrics: dict) -> str:
    if metrics["count"] >= MIN_DIAGNOSTIC_SAMPLE and metrics["direction_hit_rate"] is not None:
        if metrics["direction_hit_rate"] >= 53 and (metrics["mape"] or 999) < 2:
            return "GREEN"
        if metrics["direction_hit_rate"] < 50:
            return "RED"
    return "YELLOW"


def _rollup(groups: dict, key_name: str) -> list[dict]:
    out = []
    for key, values in groups.items():
        metrics = _metrics(values)
        out.append({key_name: key, "status": _status_for(metrics), **metrics})
    return out


def _regime(row) -> dict:
    return (row.data_quality_notes or {}).get("regime") or {"volatility": "Context not captured", "market_trend": "Context not captured"}


def _policy_rows(rows, *, return_threshold: float, confidence_threshold: float):
    return [
        row for row in rows
        if abs(float(row.predicted_return or 0.0)) >= return_threshold
        and float(row.confidence_value or 0.0) >= confidence_threshold
    ]


def abstention_policy_comparison(rows) -> dict:
    """Compare candidate policies with today's all-forecast policy.

    A candidate is an upgrade only when it has enough observations, strictly
    positive naive-baseline skill, and a material (>=3pp) direction lift.  We
    intentionally do not silently change FLAT_RETURN_THRESHOLD or inference
    behaviour from this exploratory result.
    """
    current = _error_metrics(rows)
    policies = []
    for ret in RETURN_THRESHOLDS:
        for conf in CONFIDENCE_THRESHOLDS:
            selected = _policy_rows(rows, return_threshold=ret, confidence_threshold=conf)
            metrics = _error_metrics(selected)
            coverage = round(len(selected) / len(rows), 4) if rows else 0.0
            is_current = ret == 0.0 and conf == 0.0
            improvement = None
            if current["direction_hit_rate"] is not None and metrics["direction_hit_rate"] is not None:
                improvement = round(metrics["direction_hit_rate"] - current["direction_hit_rate"], 1)
            upgrade = bool(
                not is_current
                and metrics["count"] >= MIN_POLICY_SAMPLE
                and (metrics["skill_vs_naive"] or 0) > 0
                and (improvement or 0) >= 3.0
            )
            policies.append({
                "return_threshold": ret,
                "confidence_threshold": conf,
                "coverage": coverage,
                "is_current": is_current,
                "is_upgrade": upgrade,
                "direction_lift_pp": improvement,
                **metrics,
            })
    upgrades = [p for p in policies if p["is_upgrade"]]
    return {
        "minimum_sample": MIN_POLICY_SAMPLE,
        "current": current,
        "policies": sorted(policies, key=lambda p: (not p["is_upgrade"], -(p["skill_vs_naive"] or -999), -(p["direction_lift_pp"] or -999))),
        "recommended": upgrades[0] if upgrades else None,
        "promotion_rule": "At least 30 settlements, positive skill vs unchanged-close baseline, and >=3 percentage-point directional lift vs current policy.",
    }


def next_close_diagnostics(exchange=Exchange.DSE):
    rows = list(PredictionSnapshot.objects.filter(model_family=PredictionSnapshot.ModelFamily.NEXT_CLOSE_RF, exchange=exchange, settlement_status=PredictionSnapshot.SettlementStatus.SETTLED).select_related("stock"))
    by_confidence, by_stock, by_sector, by_version, by_volatility, by_market_trend, by_served_method = (defaultdict(list) for _ in range(7))
    for row in rows:
        by_confidence[_confidence_bucket(row.confidence_value)].append(row)
        by_stock[row.stock_trading_code].append(row)
        sector = (row.stock.sector or "Unknown") if row.stock else "Unknown"
        by_sector[sector].append(row)
        by_version[row.model_version_tag or "Unknown version"].append(row)
        by_served_method[row.served_method or "unknown"].append(row)
        regime = _regime(row)
        by_volatility[regime["volatility"]].append(row)
        by_market_trend[regime["market_trend"]].append(row)
    confidence = [{"bucket": key, **_metrics(value)} for key, value in by_confidence.items()]
    stocks = _rollup(by_stock, "stock")
    sectors = _rollup(by_sector, "sector")
    versions = _rollup(by_version, "version")
    volatility_regimes = _rollup(by_volatility, "regime")
    market_trend_regimes = _rollup(by_market_trend, "regime")
    served_methods = _rollup(by_served_method, "served_method")
    all_metrics = _metrics(rows)
    warnings = []
    if len(rows) < MIN_DIAGNOSTIC_SAMPLE:
        warnings.append(f"Only {len(rows)}/{MIN_DIAGNOSTIC_SAMPLE} clean settled forecasts: results are not yet trustworthy.")
    elif all_metrics["direction_hit_rate"] is not None and all_metrics["direction_hit_rate"] < 50:
        warnings.append(f"Direction hit rate is {all_metrics['direction_hit_rate']}%, below 50%.")
    if rows:
        model_err = [abs((r.predicted_return or 0) - (r.outcome_return or 0)) for r in rows if r.outcome_return is not None]
        naive_err = [abs(r.outcome_return or 0) for r in rows if r.outcome_return is not None]
        if model_err and np.mean(model_err) >= np.mean(naive_err):
            warnings.append("Model is not currently beating the unchanged-close naive baseline on clean evidence.")
    return {
        "sample_count": len(rows),
        "minimum_sample": MIN_DIAGNOSTIC_SAMPLE,
        "confidence": confidence,
        "stocks": sorted(stocks, key=lambda x: x["stock"]),
        "sectors": sorted(sectors, key=lambda x: x["sector"]),
        "versions": sorted(versions, key=lambda x: x["version"], reverse=True),
        "volatility_regimes": sorted(volatility_regimes, key=lambda x: x["regime"]),
        "market_trend_regimes": sorted(market_trend_regimes, key=lambda x: x["regime"]),
        "served_methods": sorted(served_methods, key=lambda x: x["served_method"]),
        "policy_comparison": abstention_policy_comparison(rows),
        "ready": len(rows) >= MIN_DIAGNOSTIC_SAMPLE,
        "metrics": all_metrics,
        "warnings": warnings,
    }
