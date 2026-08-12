"""Read-only diagnostics for immutable next-close forecasts."""
from __future__ import annotations

from collections import defaultdict
import numpy as np

from market.models import Exchange, PredictionSnapshot

MIN_DIAGNOSTIC_SAMPLE = 20


def _confidence_bucket(value):
    value = float(value or 0)
    if value < .55: return "Below 55%"
    if value < .60: return "55–59%"
    if value < .65: return "60–64%"
    if value < .70: return "65–69%"
    if value < .80: return "70–79%"
    return "80%+"


def _metrics(rows):
    if not rows:
        return {"count": 0, "mape": None, "direction_hit_rate": None, "win_rate": None}
    mape = [abs((r.predicted_price - r.outcome_price) / r.outcome_price) * 100 for r in rows if r.predicted_price and r.outcome_price]
    directional = [r for r in rows if r.predicted_return is not None and r.outcome_return is not None]
    hits = [((r.predicted_return >= 0) == (r.outcome_return >= 0)) for r in directional]
    return {"count": len(rows), "mape": round(float(np.mean(mape)), 3) if mape else None, "direction_hit_rate": round(float(np.mean(hits))*100, 1) if hits else None, "win_rate": round(float(np.mean([r.outcome_return > 0 for r in directional]))*100, 1) if directional else None}


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


def next_close_diagnostics(exchange=Exchange.DSE):
    rows = list(PredictionSnapshot.objects.filter(model_family=PredictionSnapshot.ModelFamily.NEXT_CLOSE_RF, exchange=exchange, settlement_status=PredictionSnapshot.SettlementStatus.SETTLED).select_related("stock"))
    by_confidence, by_stock, by_sector = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in rows:
        by_confidence[_confidence_bucket(row.confidence_value)].append(row)
        by_stock[row.stock_trading_code].append(row)
        sector = (row.stock.sector or "Unknown") if row.stock else "Unknown"
        by_sector[sector].append(row)
    confidence = [{"bucket": key, **_metrics(value)} for key, value in by_confidence.items()]
    stocks = _rollup(by_stock, "stock")
    sectors = _rollup(by_sector, "sector")
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
        "ready": len(rows) >= MIN_DIAGNOSTIC_SAMPLE,
        "metrics": all_metrics,
        "warnings": warnings,
    }
