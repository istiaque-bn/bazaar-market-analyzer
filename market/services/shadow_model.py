"""Isolated shadow candidate: no production model writes or reads."""
from django.utils import timezone
from market.models import ShadowForecast, Stock
from market.services.close_learn import MIN_HISTORY, forecast_next_close, next_trading_day
from market.services.indicators import prices_to_df
import numpy as np

NAME = "shadow_analogue"
def run_shadow_cycle(as_of=None):
    as_of = as_of or timezone.localdate(); target = next_trading_day(as_of); made=settled=0
    for row in ShadowForecast.objects.filter(actual_close__isnull=True, target_date__lte=as_of).select_related("stock"):
        actual = row.stock.prices.filter(date=row.target_date).values_list("close", flat=True).first()
        if actual is not None: row.actual_close=float(actual); row.settled_at=timezone.now(); row.save(update_fields=["actual_close","settled_at"]); settled+=1
    for stock in Stock.objects.filter(is_active=True):
        df=prices_to_df(stock.prices.live().filter(date__lte=as_of).order_by("date"))
        if len(df)<MIN_HISTORY or ShadowForecast.objects.filter(stock=stock,target_date=target,candidate_name=NAME).exists(): continue
        pred=forecast_next_close(df, return_bias=0.0, exchange=stock.exchange, sector=stock.sector or "")
        if pred: ShadowForecast.objects.create(stock=stock,as_of=as_of,target_date=target,last_close=pred["last_close"],predicted_close=pred["predicted_close"],predicted_return=pred["predicted_return"],candidate_name=NAME); made+=1
    return {"ok":True,"created":made,"settled":settled}

def shadow_report():
    rows=list(ShadowForecast.objects.filter(candidate_name=NAME,actual_close__isnull=False).order_by("-target_date")[:10000])
    if not rows: return {"n":0}
    actual=np.array([r.actual_close for r in rows]); pred=np.array([r.predicted_close for r in rows]); base=np.array([r.last_close for r in rows])
    mae=float(np.mean(abs(pred-actual))); base_mae=float(np.mean(abs(base-actual))); skill=None if base_mae==0 else 1-mae/base_mae
    actual_ret=(actual-base)/base; pred_ret=(pred-base)/base
    directional=actual_ret!=0; direction=float(np.mean(np.sign(actual_ret[directional])==np.sign(pred_ret[directional]))) if directional.any() else None
    return {"n":len(rows),"mae":round(mae,4),"naive_mae":round(base_mae,4),"skill":None if skill is None else round(float(skill),4),"direction":None if direction is None else round(direction,4)}
