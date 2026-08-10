# Bazaar Market Analyzer — Page Guide

This guide explains the main pages in simple language. It can be shared with users, staff, and administrators.

> Bazaar is a research tool. Signals, forecasts, backtests, and ML results are not guaranteed investment advice or a promise of profit.

## Normal user pages

### Market Dashboard

- **Buy signals**: shares the system currently sees as more positive.
- **Sell signals**: shares showing weakness or higher risk.
- **Research candidates**: shares worth researching further; not guaranteed buys.
- **Watch**: shares with mixed signals; wait for a clearer direction.
- **Data last updated**: the latest verified DSE data. When the market is closed, the page also shows the next opening time.
- **Market status**: whether DSE is open or closed.
- **Model status**: whether ML currently has useful evidence or whether the system is mainly using rule-based analysis.
- **Share lists**: quick lists of potential, safer, and caution shares for research.

### Stocks

- **Search and filters**: find a share by code, sector, signal, or exchange.
- **Price and change**: the latest known price movement.
- **Signal badge**: a simple BUY, SELL, WATCH, or HOLD summary.
- **Score and confidence**: how strongly the available indicators support a signal.
- **Watchlist button**: saves a share for the user to monitor later.

### Stock detail

- **Price chart**: shows price movement over time.
- **Open vs. close chart**: compares the opening and closing price for each day.
- **Volume chart**: shows how actively a share was traded.
- **Technical indicators**: RSI, moving averages, MACD, volatility, and trend information.
- **Expected price / prediction**: a historical-pattern estimate, not a promise.
- **Signal explanation**: explains why the system marked the share as Buy, Sell, Watch, or Hold.
- **Data-quality note**: explains when the displayed price is the latest verified close rather than a live quote.

### Watchlist

- Shows a user's saved shares.
- Gives a quick view of recent price, signal, and confidence.
- Lets the user remove a saved share.

### Portfolio

- **Portfolio value**: estimated value of holdings entered by the user.
- **Investment and profit/loss**: compares purchase value with the latest verified price.
- **Holdings**: quantities, average cost, current value, and gain/loss.
- **Transactions**: lets users add, edit, or delete their own buy and sell records.
- **Quote freshness**: says whether prices are live, delayed, or from a closed market.

### Backtests

- Shows how a strategy would have performed on past data.
- **Return**: simulated profit or loss.
- **Win rate**: percentage of profitable simulated trades.
- **Drawdown**: the largest temporary loss in the simulation.
- Past results do not guarantee future profit.

### Alerts

- Shows personal market and system notifications.
- Helps users see what is new or already read.

### Feedback

- **Submit feedback**: report bugs, incorrect data, prediction concerns, ideas, or account issues.
- **My feedback**: track reports submitted by the user.
- **Follow-up**: add more details to an open report.
- **Response**: read replies from the team.
- **Dispute resolution**: ask for another review if a user believes an issue was closed incorrectly.

### Profile

- Update personal account details.
- Manage available notification choices.
- Change the account password.

## Staff pages

### Staff Panel

- **User count**: number of regular users.
- **Market status**: DSE opening and closing status.
- **Operational alerts**: warnings about data, jobs, or system health.
- **Recent task runs**: recently completed background jobs.
- **Feedback triage**: review and respond to most user feedback.

### Feedback Triage

- Filter feedback by category, status, priority, date, assignment, page, or user.
- Move items through New, Under Review, In Progress, and closed statuses.
- Add internal team-only notes.
- Assign an item to yourself or another team member.
- Reply to the user.
- Sensitive account issues are restricted to administrators.

## Admin pages

### Admin Panel

- **User cards**: total, active, inactive, staff, and admin accounts.
- **Automation cards**: whether market sync, analysis, daily data append, and ML training are enabled.
- **Manual controls**: safely queue DSE fetching, analysis, end-of-day processing, and ML training.
- **Task health**: latest success and failure for important background jobs.
- **Active model**: the approved ML model and when it trained.
- **Telegram ML report**: delivery status of the daily admin report.
- **Operational alerts**: warnings about stale data, failed jobs, stuck jobs, database problems, or model quality.
- **Feedback summary**: quick count of new and urgent feedback.

### ML Reliability

- **Training at a glance**: says whether ML training succeeded, skipped normally, is running, failed, or is disabled.
- **Normal skip explanation**: the 10-day model needs future outcomes before new data can be used for training.
- **Active models**: model name, market, training time, newest data used, and training-row count.
- **Healthy, Watch, and Degraded badges**: a simple model-health summary.
- **Accuracy and balanced accuracy**: how often the model gets direction right.
- **Skill vs. baseline**: whether ML is better than a simple naive prediction.
- **Calibration**: whether confidence values match real outcomes.
- **Performance trend chart**: whether reliability is improving or worsening.
- **Economic diagnostics**: an after-cost sanity check, not an investment guarantee.
- **Drift warnings**: warns when current market behaviour differs from the model's learned environment.

### Autonomous Paper Trading

- This is a simulation only; it never uses real money or a broker account.
- **Starting cash and available cash**: virtual money used by the simulator.
- **Total equity**: virtual cash plus current virtual holding value.
- **Total return**: simulated profit/loss after estimated fees and slippage.
- **Open positions**: current virtual holdings.
- **Portfolio performance chart**: daily simulated equity trend.
- **Virtual trade log**: every simulated buy/sell and its reason.
- **Learning feedback**: win rate and average after-cost result of closed virtual trades.

### Operations Report

- **Firing alerts**: current problems needing attention.
- **Task health**: job success, failure, delay, and stuck-task information.
- **Data freshness**: whether DSE data and analysis are current.
- **Model health**: whether live ML performance is better than a simple baseline.
- This page is for technical support and server monitoring, not normal users.

### Data Quality

- **Data source**: where price data came from.
- **Freshness**: how old the latest data is.
- **Rejected records**: rows rejected because of invalid or suspicious values.
- **Synthetic/fallback data**: clearly identifies when real upstream data was unavailable.

### Feedback Dashboard

- **New, urgent, and in-progress feedback**: quick workload summary.
- **Bug and feature-request counts**: helps prioritize development work.
- **Average review time**: shows how quickly reports are handled.
- **Frequently reported pages**: identifies where users need the most help.
- **CSV export**: creates a safe planning summary without exposing sensitive report details.

## Suggested user-facing message

> Bazaar Market Analyzer helps you study DSE shares using market data, technical indicators, and experimental machine learning. Signals and forecasts are research tools, not guaranteed investment advice. Always make your own decision and consider risk.
