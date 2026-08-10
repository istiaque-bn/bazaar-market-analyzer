# Next-Day Close Learner audit — 2026-08-11

## Scope and decision

This is a read-only audit of `market.services.close_learn`. No learner, primary direction model, Celery schedule, cache/model artifact, database row, or VPS setting was changed.

**Decision: stop before challenger optimisation.** A trustworthy frozen `PRODUCTION_BASELINE_V1` cannot yet be reconstructed from the available immutable records. The provenance gaps below must be fixed before a challenger is trained or compared.

## Live reference measured from the development database

| Measure | Value |
|---|---:|
| Settled next-close forecasts | 4,592 |
| Pending next-close forecasts | 1,921 |
| Model MAPE | 2.1578% |
| Naive unchanged-close MAPE | 1.9889% |
| Skill vs naive (return MAE) | -8.41% |
| Forecasts beating naive | 33.08% |
| Direction hit rate | 43.41% |
| Candidate (before serving fallback) skill | -7.22% |
| Liquid universe | 80 shares |
| Active stock-specific biases | 15 |
| Global bias | -0.000742 |

These values differ from the supplied approximate figures because more forecasts have settled. They reproduce `compute_skill_metrics`, not an independent historical rerun.

## System audit

1. **Target:** training uses simple next-session return, `close[t+1] / close[t] - 1`, then converts it to predicted close. It is not a raw-price regressor.
2. **Prediction-time data:** current stock bar through `as_of`, plus same-session exchange/sector aggregates; intended to run after close.
3. **Features:** RSI, MACD histogram, 1/2/3/5/20-day returns, volatility, volume ratio, SMA distances, index/sector/breadth/relative return, intraday return, gap, ATR range, close location, volume/turnover acceleration, and streak.
4. **Context:** “index” is equal-weight mean stock return; breadth is fraction positive; sector is equal-weight peer return.
5. **History:** 396 active DSE stocks have 437–1,537 stored rows each.
6. **Missing values:** infinities become missing; incomplete feature rows are dropped. Median imputation is fit on the training fold only. Missing context defaults to zero return / 0.5 breadth.
7. **Corporate actions:** price rows carry adjustment status, but learner code does not adjust/exclude corporate-action rows itself.
8. **Illiquidity:** training needs positive next-session volume and positive volume acceleration; settlement skips zero-volume bars.
9. **Liquid universe:** top 80 by average volume over the latest 45 calendar days; recomputed on every call, not frozen historically and not turnover/value based.
10. **Biases:** global and stock biases are capped ±1% EMAs of prediction-return error, subtracted from future predictions.
11. **15 stock biases:** a liquid stock needs at least 8 settlements; 15 states currently meet it.
12. **Settlement:** writes target-day actual close and price/percentage/return error when a positive-close, non-zero-volume bar is present.
13. **Immutability:** `NextDayCloseForecast` is mutable because creation uses `update_or_create(stock, target_date)`. A rerun can replace a pending forecast; backfill can reset historical fields. `PredictionSnapshot` exists for immutable capture, but this database has **zero** `next_close_rf` snapshots.
14. **Timestamp safety:** ordinary live forecasts filter bars through `as_of`, but mutable rows do not prove original input state. Backfill can use a model trained after the historical forecast date.
15. **Algorithm:** current training selects a 3-class down/flat/up classifier, translating probabilities to return. It compares Logistic Regression, Random Forest, and XGBoost; legacy code supports a two-stage Random Forest return model.
16. **Parameters:** Logistic Regression `max_iter=1500`; RF `120 trees/depth 8/min leaf 12`; XGBoost `160 trees/depth 4/lr .04/subsample .8/colsample .8/L1 .2/L2 2`. Flat band ±0.5%; abstention 65%.
17. **Retraining:** every post-close settlement cycle when anything settles.
18. **Window:** candidate selection tests 365/730/1095-day rolling windows; final fit uses selected window.
19. **Shuffling:** final evaluation uses chronological walk-forward folds, not random shuffle.
20. **Leakage controls:** 3-calendar-day embargo for a one-day label; fold-local imputer. However present-day liquid selection and future-trained backfill model prevent a clean historical claim.
21. **Scaling:** no scaler; fold-local median imputer.
22. **Context leakage:** same-day context is safe only after full close; historical rows lack evidence of that availability.
23. **Bias leakage:** live updating is chronological, but historical backfill can use a later-trained model.
24. **Tuning leakage:** no untouched final holdout is reserved; walk-forward folds are used for selection and activation.
25. **Metrics:** MAPE is close error / actual close; MAE and skill use return error; skill = `1 - model_MAE / naive_MAE`; beats-naive is strict lower return error; direction is return-sign match.

## Data-quality observations

- All 396 active DSE stocks have blank sector. Sector return is therefore not real sector information.
- 504,736 live price rows have quality flags; 3,045 rows have unknown provenance/adjustment state. Flag distribution needs review before research.
- The top-80 universe has survivorship/look-ahead risk because it uses current volume.

## Required Phase-2 repairs before any challenger

1. Capture immutable next-close snapshots at forecast time with version, schema, inputs, prediction, naive baseline, and timestamp.
2. Make a forecast append-only after creation; only settlement fields may change.
3. Freeze historical liquid membership by each as-of date, preferably using traded value and session coverage.
4. Repair DSE sector data or omit sector features explicitly.
5. Reserve a final untouched chronological holdout before feature/model/window tuning, then reproduce the baseline on it.
6. Add leakage tests for future model, volume, and context use.

Only after these repairs can raw-price, simple-return, log-return, and direction challengers be compared fairly.
