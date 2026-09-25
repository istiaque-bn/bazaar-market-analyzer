import json
import numpy as np
import pandas as pd
from django.core.management.base import BaseCommand
from market.models import Exchange
from market.services.ml_model import FEATURE_COLS, _binary_model, build_training_panel
from market.services.ml_training import fit_median_imputer, apply_imputer, recency_weights, classification_metrics

class Command(BaseCommand):
    help = "Evaluate a regime-routed, threshold-tuned forward challenger."
    def handle(self, *args, **kwargs):
        p=build_training_panel(Exchange.DSE, limit_stocks=120); end=p.date.max(); ts=end-pd.Timedelta(days=30); vs=ts-pd.Timedelta(days=60)
        train=p[(p.date>=vs-pd.Timedelta(days=379))&(p.date<=vs-pd.Timedelta(days=14))]
        val=p[(p.date>=vs)&(p.date<ts)]; fit=p[(p.date>=vs-pd.Timedelta(days=379))&(p.date<=ts-pd.Timedelta(days=14))]; test=p[p.date>=ts]
        missing=sorted(set(test.trend_regime.unique())-set(val.trend_regime.unique()))
        if missing:
            self.stdout.write(json.dumps({'ok':False,'error':'validation lacks regimes present in final test','missing_regimes':missing,'validation_distribution':val.trend_regime.value_counts().to_dict(),'test_distribution':test.trend_regime.value_counts().to_dict()},indent=2))
            return
        val_out=np.full(len(val),np.nan); test_out=np.full(len(test),np.nan); detail={}
        for regime in sorted(p.trend_regime.unique()):
            tr=train[train.trend_regime==regime]; va=val[val.trend_regime==regime]; ft=fit[fit.trend_regime==regime]; te=test[test.trend_regime==regime]
            if min(len(tr),len(ft))<100 or len(va)<20 or len(te)<20: continue
            imp=fit_median_imputer(tr[FEATURE_COLS]); m=_binary_model('logistic'); m.fit(apply_imputer(imp,tr[FEATURE_COLS]),tr.label,sample_weight=recency_weights(tr.date,half_life_days=60))
            pv=m.predict_proba(apply_imputer(imp,va[FEATURE_COLS]))[:,list(m.classes_).index(1.0)]
            best=max(np.arange(.2,.81,.05),key=lambda t: classification_metrics(va.label,(pv>=t).astype(int),pv).get('balanced_accuracy') or 0)
            imp=fit_median_imputer(ft[FEATURE_COLS]); m=_binary_model('logistic'); m.fit(apply_imputer(imp,ft[FEATURE_COLS]),ft.label,sample_weight=recency_weights(ft.date,half_life_days=60))
            pt=m.predict_proba(apply_imputer(imp,te[FEATURE_COLS]))[:,list(m.classes_).index(1.0)]
            val_out[val.index.get_indexer(va.index)]=pv
            test_out[test.index.get_indexer(te.index)]=pt
            detail[str(regime)]={'threshold':float(best),'validation_n':len(va),'test_n':len(te)}
        mask=~np.isnan(test_out); thresholds=np.array([detail[str(r)]['threshold'] if str(r) in detail else .5 for r in test.loc[mask,'trend_regime']])
        prob=test_out[mask]; confidence=np.abs(prob-thresholds); covered=confidence>=.08
        y=test.loc[mask,'label'].to_numpy(); pred=(prob>=thresholds).astype(int)
        result={'ok':True,'regimes':detail,'all':classification_metrics(y,pred,prob),'covered':classification_metrics(y[covered],pred[covered],prob[covered]),'coverage':round(float(covered.mean()),4),'positive_rate':round(float(y.mean()),4)}
        self.stdout.write(json.dumps(result,indent=2))
