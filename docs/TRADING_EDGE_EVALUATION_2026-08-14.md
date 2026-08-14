# Trading-edge evaluation — 2026-08-14

## Decision

**Do not deploy the strict candidate.**  It does not clear the pre-registered
upgrade test on the locked final holdout.  The production backtest default
remains `rsi_macd_v1`; the production paper account remains
`three_day_book_rules`.

`strict_research_v1` is present only as an opt-in research candidate.  It is
not selected by `run_backtest()` defaults, paper-account defaults, or the
screener.

## Pre-registered candidate (not tuned on the holdout)

- Entry: RSI(14) < 35 **and** fresh MACD bullish cross **and** volume >= 1.25
  × trailing 20-session volume, with frozen-universe breadth >= 55% above
  SMA(50).
- Universe: top 40 DSE/CSE names by a trailing traded-value proxy as of the
  day before the test starts; Z group, unknown-provenance rows, flagged OHLC,
  zero-range sessions and unfillable fills are excluded.
- Exit: 6% stop (triggered by the daily low, executed next tradable open),
  12% target, or 40 sessions.
- Portfolio: up to five positions, 12% target size, same `CostConfig` as the
  deployed backtest (0.30% brokerage + 0.05% tax + 0.10% spread + 0.10%
  slippage per side).
- Learned models: `next_close_rf` is not imported or consulted.  The paper
  candidate treats forward-return probability as an existing eligibility
  threshold only and ranks by transparent rule score/confidence, not model
  probability.

## Locked holdout

The final 90 calendar days available at evaluation time were locked as
**2026-05-15 through 2026-08-13**.  Candidate parameters above were fixed
before this run.  The comparison uses DSE because CSE is disabled in the
deployment and its legacy OHLC quality gate leaves too little eligible data.

| Strategy | Stocks | Trades | Total costs (BDT) | Net return | Sharpe | Max drawdown | Same-universe buy/hold |
|---|---:|---:|---:|---:|---:|---:|---:|
| Deployed `rsi_macd_v1` | 396 | 94 | 49,626.33 | +9.472% | 3.249 | -2.826% | +20.021% |
| Candidate `strict_research_v1` | 40 | 0 | 0.00 | 0.000% | — | 0.000% | +2.189% |

The candidate has lower turnover, but zero trades cannot demonstrate a
tradable edge.  It is also not strictly better than the deployed strategy or
buy-and-hold, so it fails the upgrade criteria.

## Same costed one-year baseline window

Window: **2025-08-14 through 2026-08-13**.  These are reference results, not
a profitability claim.

| Exchange | Deployed trades | Total costs (BDT) | Net return | Sharpe | Max drawdown | Same-universe buy/hold |
|---|---:|---:|---:|---:|---:|---:|
| DSE | 300 | 133,585.27 | -5.689% | -0.278 | -24.309% | +27.074% |
| CSE | 248 | 21,819.82 | +3.263% | +1.344 | -1.830% | +24.707% |

The candidate produced zero CSE trades in the one-year check (14 eligible
names after quality/provenance gates).  This reinforces the non-deployment
decision; it is not evidence that CSE is untradable, only that the candidate
and the available clean CSE data do not support a claim.

## Data and safety controls added for the candidate

- The live default is untouched; `rsi_macd_v1` retains its historical
  behavior for meaningful baseline comparison.
- Candidate universe membership is frozen before each window, avoiding
  today's liquid names leaking into historical selection.
- Candidate bars exclude invalid OHLC / abnormal-jump flags and unknown
  provenance.  `open_out_of_range` is additionally excluded because a strict
  next-open fill cannot be justified from that bar.
- Any trade touching a >=25% single-day move remains excluded from headline
  P&L as a possible split/rights event, consistent with the existing engine.
- No broker or order-execution capability was added.

## Next research step

Do not relax thresholds using this holdout.  First repair/split-adjust CSE
history and obtain reliable adjusted corporate-action data.  Then define a
new, later untouched holdout and test one research-window-selected candidate
with enough signals to be statistically meaningful.  Until then, continue to
describe all signals and paper results as experimental research, not an edge
or a profit expectation.
