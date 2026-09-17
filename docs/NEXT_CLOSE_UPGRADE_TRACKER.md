# Next-Day Close Learner Upgrade Tracker

Update this file after each meaningful phase. The primary direction model must
remain unchanged unless separately evaluated.

| Phase | Status | Notes |
|---:|---|---|
| 1. Audit | Complete | `docs/NEXT_CLOSE_AUDIT_2026-08-11.md` records target, features, leakage risks and baseline figures. |
| 2. Frozen baseline | Collecting | New immutable forecasts are now captured; wait for clean settled evidence. |
| 3. Target comparison | Not started | Compare raw close, return, log-return, direction only after baseline. |
| 4. Features / ablation | Not started | Must use final holdout. |
| 5. Regime features | Not started | Research only. |
| 6. Algorithm benchmark | In progress | Selective absolute-error HGB benchmarked on identical chronological periods; failed locked holdout. |
| 7. Global/sector/stock architecture | Not started | Sector path blocked by missing DSE sectors. |
| 8. Walk-forward + holdout | Complete for HGB v1 | Three research folds passed weakly, but the locked 90-day holdout rejected the candidate. |
| 9. Safety / leakage | Core complete | Append-only forecasts, immutable snapshots, as-of liquidity, no future-trained backfill model. |
| 10. Confidence system | Collecting | Immutable confidence diagnostics deployed; needs settled forecasts. |
| 11. Flat / no-signal zone | Not started | Tune only on research data. |
| 12. Evaluation metrics | Partly complete | Existing metrics + immutable diagnostics; challenger scorecard pending. |
| 13. Stock diagnostics | Collecting | Green/Yellow/Red diagnostics deployed; needs sample size. |
| 14. Sector diagnostics | Blocked | All active DSE sector values were blank at audit. |
| 15. Hyperparameter tuning | Not started | Time-series validation only. |
| 16. Feature ablation | Not started | Must preserve final holdout. |
| 17. Challenger scorecard | HGB v1 rejected | Research skill +0.15%; locked-holdout skill -0.21%. Direction 58.42%, but MAE gate failed. |
| 18. Promotion rules | Defined | Must beat naive baseline consistently. |
| 19. Shadow mode | Not started | Best accepted research challenger only. |
| 20. Staged promotion | Not started | Research → Shadow → secondary signal; rollback required. |
| 21. Reliability dashboard | Complete | ML Reliability page shows immutable next-close evidence. |
| 22. Deterioration warnings | Complete | Read-only warnings for insufficient evidence, poor direction, and naive underperformance. |
| 23. Data quality | Partial | Summary/gates added; verified DSE sector source still required. |
| 24. Final decision | Not started | Compare baseline, challenger and naive on untouched holdout. |

## Current deployed safeguards

- Forecast retries cannot overwrite an existing next-close forecast.
- New forecasts create immutable evidence records at prediction time.
- Historical backfill does not load a later-trained model.
- Sector return is zeroed until DSE sector coverage is reliable.
- The ML Reliability page shows immutable confidence, stock, and health data.

## Next action

Keep collecting clean shadow outcomes. The selective HGB v1 research
challenger (`evaluate_next_close_challenger`) must not enter shadow mode: on
the VPS dataset its pre-holdout research skill was only +0.15% and its locked
90-day holdout skill was -0.21%, despite 58.42% directional accuracy. The
next challenger must be specified under a new name and clear both the 2%
research MAE-skill gate and the 2% locked-holdout gate before live shadow
collection begins.
