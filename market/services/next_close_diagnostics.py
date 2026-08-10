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


def next_close_diagnostics(exchange=Exchange.DSE):
    rows = list(PredictionSnapshot.objects.filter(model_family=PredictionSnapshot.ModelFamily.NEXT_CLOSE_RF, exchange=exchange, settlement_status=PredictionSnapshot.SettlementStatus.SETTLED).select_related("stock"))
    by_confidence, by_stock = defaultdict(list), defaultdict(list)
    for row in rows:
        by_confidence[_confidence_bucket(row.confidence_value)].append(row)
        by_stock[row.stock_trading_code].append(row)
    confidence = [{"bucket": key, **_metrics(value)} for key, value in by_confidence.items()]
    stocks = []
    for code, values in by_stock.items():
        metrics = _metrics(values)
        status = "YELLOW"
        if metrics["count"] >= MIN_DIAGNOSTIC_SAMPLE and metrics["direction_hit_rate"] is not None:
            status = "GREEN" if metrics["direction_hit_rate"] >= 53 and (metrics["mape"] or 999) < 2 else "RED" if metrics["direction_hit_rate"] < 50 else "YELLOW"
        stocks.append({"stock": code, "status": status, **metrics})
    return {"sample_count": len(rows), "minimum_sample": MIN_DIAGNOSTIC_SAMPLE, "confidence": confidence, "stocks": sorted(stocks, key=lambda x: x["stock"]), "ready": len(rows) >= MIN_DIAGNOSTIC_SAMPLE}
