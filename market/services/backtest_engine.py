"""
Phase 5 — portfolio-level backtest engine.

Replaces the old per-stock, cost-free, same-bar-execution simulator with a
single shared-cash portfolio that walks forward day by day:

  - Signals are read off a stock's indicator row on day d (data available
    as of that day's close only — compute_indicators is a purely causal
    rolling computation, so precomputing it once over a stock's full
    history and only ever reading row d is safe).
  - Orders resulting from a day-d signal are scheduled for and executed at
    the OPEN of that stock's own next trading session — never the same bar
    the signal fired on.
  - Every fill pays configurable brokerage + tax (on notional) and
    spread + slippage (as an adverse price move) — see CostConfig.
  - A fill is rejected or sized down against a configurable liquidity cap
    (% of that session's traded volume) and available cash.
  - The engine has no adjusted-close / corporate-action data source. It
    flags (and excludes from headline metrics) any trade whose entry or
    exit lands on a day with an implausible single-day return, since that
    almost always means an unadjusted split/rights issue rather than a
    real, tradable price move — see CORP_ACTION_RETURN_THRESHOLD_PCT.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from market.models import DataSource, Stock, StockGroup
from market.services.data_quality import TRAINING_EXCLUDE_FLAGS
from market.services.indicators import compute_indicators, prices_to_df

MIN_HISTORY_BARS = 100
CORP_ACTION_RETURN_THRESHOLD_PCT = 25.0
ENTRY_RETRY_SESSIONS = 5
TRADING_DAYS_PER_YEAR = 250  # BD week is Sun-Thu (~5 sessions x ~50 weeks)
MIN_SAMPLE_FOR_BREAKDOWN = 20


@dataclass
class CostConfig:
    """Per-side costs, in percent of trade notional unless noted. Defaults
    are illustrative approximations for a BD retail brokerage account —
    not tax/legal advice. Override for your actual broker/exchange terms."""

    brokerage_pct: float = 0.30
    tax_pct: float = 0.05
    spread_pct: float = 0.10
    slippage_pct: float = 0.10
    liquidity_limit_pct: float = 5.0
    min_liquidity_shares: int = 100

    @property
    def adverse_price_pct(self) -> float:
        return (self.spread_pct + self.slippage_pct) / 100.0

    @property
    def fee_pct(self) -> float:
        return (self.brokerage_pct + self.tax_pct) / 100.0

    def as_dict(self) -> dict:
        return {
            "brokerage_pct": self.brokerage_pct,
            "tax_pct": self.tax_pct,
            "spread_pct": self.spread_pct,
            "slippage_pct": self.slippage_pct,
            "liquidity_limit_pct": self.liquidity_limit_pct,
            "min_liquidity_shares": self.min_liquidity_shares,
        }


@dataclass
class PortfolioConfig:
    initial_cash: float = 1_000_000.0
    position_size_pct: float = 5.0
    max_positions: int = 20
    hold_days: int = 20
    target_return_pct: float = 5.0
    # ``rsi_macd_v1`` is deliberately the deployed baseline.  The strict
    # candidate is opt-in until an untouched holdout has demonstrated an edge.
    strategy: str = "rsi_macd_v1"
    stop_loss_pct: float | None = None
    volume_confirmation_ratio: float = 1.25
    min_traded_value: float = 250_000.0
    universe_size: int = 40
    breadth_threshold: float = 0.55

    def as_dict(self) -> dict:
        return {
            "initial_cash": self.initial_cash,
            "position_size_pct": self.position_size_pct,
            "max_positions": self.max_positions,
            "hold_days": self.hold_days,
            "target_return_pct": self.target_return_pct,
            "strategy": self.strategy,
            "stop_loss_pct": self.stop_loss_pct,
            "volume_confirmation_ratio": self.volume_confirmation_ratio,
            "min_traded_value": self.min_traded_value,
            "universe_size": self.universe_size,
            "breadth_threshold": self.breadth_threshold,
        }


@dataclass
class StockSeries:
    stock_id: int
    trading_code: str
    exchange: str
    sector: str
    frame: pd.DataFrame
    dates: list = field(default_factory=list)
    date_to_idx: dict = field(default_factory=dict)
    window_dates: list = field(default_factory=list)


@dataclass
class _Position:
    stock_id: int
    exchange: str
    sector: str
    entry_date: pd.Timestamp
    entry_signal_date: pd.Timestamp
    entry_open_price: float
    entry_exec_price: float
    shares: int
    entry_fee: float
    entry_slippage_cost: float
    cash_paid: float
    last_price: float
    last_price_date: pd.Timestamp
    corp_action_flagged: bool = False


@dataclass
class _PendingOrder:
    stock_id: int
    side: str  # "buy" / "sell"
    signal_date: pd.Timestamp
    target_date: pd.Timestamp
    attempts_left: int
    reason: str = ""
    position: "_Position | None" = None


def _signal_at(row: pd.Series, prev_row: pd.Series | None, *, strategy: str = "rsi_macd_v1", breadth_ok: bool = True, volume_confirmation_ratio: float = 1.25) -> bool:
    """Same rule as the pre-Phase-5 engine: RSI oversold or a fresh MACD
    bullish cross, evaluated on day d's own indicator row only."""
    rsi = row.get("rsi_14")
    macd_up = (
        prev_row is not None
        and pd.notna(row.get("macd"))
        and pd.notna(row.get("macd_signal"))
        and pd.notna(prev_row.get("macd"))
        and pd.notna(prev_row.get("macd_signal"))
        and prev_row["macd"] <= prev_row["macd_signal"]
        and row["macd"] > row["macd_signal"]
    )
    if strategy == "rsi_macd_v1":
        return bool((pd.notna(rsi) and rsi < 35) or macd_up)
    if strategy != "strict_research_v1":
        raise ValueError(f"Unknown backtest strategy: {strategy}")
    # The candidate intentionally waits for a reversal confirmation, not just
    # an oversold print: both RSI and a fresh MACD cross plus above-normal
    # volume.  A positive market breadth regime prevents mean-reversion buys
    # during broad market declines.  All fields are from the signal bar.
    volume = row.get("volume")
    volume_sma = row.get("volume_sma_20")
    volume_ok = pd.notna(volume) and pd.notna(volume_sma) and volume_sma > 0 and volume >= volume_sma * volume_confirmation_ratio
    return bool(pd.notna(rsi) and rsi < 35 and macd_up and volume_ok and breadth_ok)


def _load_universe(exchange: str | None, end_date: date, include_demo: bool = False, *, strict: bool = False, as_of_date: date | None = None, min_traded_value: float = 0, universe_size: int = 40) -> list[StockSeries]:
    qs = Stock.objects.filter(is_active=True)
    if exchange:
        qs = qs.filter(exchange=exchange)
    out = []
    # Candidate membership is selected once using information available before
    # the tested window. It is never recomputed from today's liquid names.
    if strict:
        qs = qs.exclude(group=StockGroup.Z)
    ranked: list[tuple[float, Stock]] = []
    for stock in qs:
        if strict:
            cutoff = as_of_date or end_date
            trailing = stock.prices.live().exclude(source=DataSource.UNKNOWN).filter(date__lte=cutoff).order_by("-date")[:60]
            # Some legacy feeds stored a non-zero but clearly non-notional
            # ``value`` field.  Use the conservative larger of supplied value
            # and close × volume as the traded-value proxy, never raw volume.
            values = [max(float(p.value or 0), float(p.close or 0) * float(p.volume or 0)) for p in trailing if not set(p.quality_flags or []) & (TRAINING_EXCLUDE_FLAGS | {"open_out_of_range"})]
            if len(values) < 40 or (sum(values) / len(values)) < min_traded_value:
                continue
            ranked.append((sum(values) / len(values), stock))
        else:
            ranked.append((0, stock))
    if strict:
        ranked.sort(key=lambda item: item[0], reverse=True)
        ranked = ranked[:universe_size]
    for _, stock in ranked:
        prices_qs = stock.prices.all() if include_demo else stock.prices.live()
        if strict:
            # Unknown-provenance rows are excluded from the candidate rather
            # than silently treated as evidence of a tradable fill.
            prices_qs = prices_qs.exclude(source=DataSource.UNKNOWN)
            df = prices_to_df(prices_qs.filter(date__lte=end_date), exclude_quality_flags=TRAINING_EXCLUDE_FLAGS | {"open_out_of_range"})
        else:
            df = prices_to_df(prices_qs.filter(date__lte=end_date))
        if len(df) < MIN_HISTORY_BARS:
            continue
        ind = compute_indicators(df)
        if ind.empty:
            continue
        ind = ind.copy()
        ind["date"] = pd.to_datetime(ind["date"]).dt.normalize()
        ind = ind.set_index("date").sort_index()
        dates = list(ind.index)
        out.append(
            StockSeries(
                stock_id=stock.id,
                trading_code=stock.trading_code,
                exchange=stock.exchange,
                sector=stock.sector or "Unknown",
                frame=ind,
                dates=dates,
                date_to_idx={dt: i for i, dt in enumerate(dates)},
            )
        )
    return out


class PortfolioBacktester:
    def __init__(
        self,
        exchange: str | None,
        start_date: date,
        end_date: date,
        cost_config: CostConfig,
        portfolio_config: PortfolioConfig,
        include_demo: bool = False,
    ):
        if start_date > end_date:
            raise ValueError("start_date must be <= end_date")
        self.exchange = exchange
        self.start_date = pd.Timestamp(start_date)
        self.end_date = pd.Timestamp(end_date)
        self.cost = cost_config
        self.pf = portfolio_config
        self.include_demo = include_demo

    def _stock_next_date(self, s: StockSeries, after: pd.Timestamp) -> pd.Timestamp | None:
        i = bisect.bisect_right(s.dates, after)
        if i < len(s.dates) and s.dates[i] <= self.end_date:
            return s.dates[i]
        return None

    def run(self) -> dict:
        strict = self.pf.strategy == "strict_research_v1"
        universe = _load_universe(
            self.exchange, self.end_date.date(), include_demo=self.include_demo,
            strict=strict, as_of_date=(self.start_date - pd.Timedelta(days=1)).date(),
            min_traded_value=self.pf.min_traded_value, universe_size=self.pf.universe_size,
        )
        window_all_dates: set = set()
        for s in universe:
            s.window_dates = [d for d in s.dates if self.start_date <= d <= self.end_date]
            window_all_dates.update(s.window_dates)
        universe = [s for s in universe if s.window_dates]

        if not universe:
            return {
                "ok": False,
                "error": "No stock has enough history (>=100 bars) with an observation inside the requested date range.",
            }

        by_stock = {s.stock_id: s for s in universe}
        calendar = sorted(window_all_dates)
        data_start_date = calendar[0].date()
        data_end_date = calendar[-1].date()

        cash = self.pf.initial_cash
        positions: dict[int, _Position] = {}
        pending: list[_PendingOrder] = []
        trades: list[dict] = []
        equity_curve: list[dict] = []
        warnings_log: list[str] = []
        rejections = {"insufficient_liquidity_or_cash": 0, "max_positions": 0, "already_open": 0, "entry_abandoned_suspended": 0}

        # Breadth is a frozen-universe, same-day diagnostic.  It deliberately
        # uses no membership or returns from after the decision date.
        breadth_by_date: dict[pd.Timestamp, float] = {}
        if strict:
            for d in calendar:
                in_uptrend = []
                for s in universe:
                    if d not in s.frame.index:
                        continue
                    row = s.frame.loc[d]
                    if pd.notna(row.get("sma_50")):
                        in_uptrend.append(float(row["close"]) > float(row["sma_50"]))
                breadth_by_date[d] = float(np.mean(in_uptrend)) if in_uptrend else 0.0

        def open_position(order: _PendingOrder, s: StockSeries, d: pd.Timestamp, row: pd.Series) -> bool:
            nonlocal cash
            open_px = float(row["open"])
            volume = float(row.get("volume") or 0)
            if open_px <= 0:
                return False
            # A zero-range session or an explicit bad-OHLC flag cannot support
            # the assumed next-open fill. Reject rather than invent liquidity.
            if strict and (float(row.get("high") or 0) <= float(row.get("low") or 0)):
                rejections["insufficient_liquidity_or_cash"] += 1
                return False
            exec_price = open_px * (1 + self.cost.adverse_price_pct)
            equity_now = cash + sum(p.shares * p.last_price for p in positions.values())
            notional_target = equity_now * self.pf.position_size_pct / 100.0
            shares_by_size = int(notional_target // exec_price)
            shares_by_liquidity = int(volume * self.cost.liquidity_limit_pct / 100.0)
            shares_by_cash = int(cash // (exec_price * (1 + self.cost.fee_pct)))
            shares = max(0, min(shares_by_size, shares_by_liquidity, shares_by_cash))
            if volume < self.cost.min_liquidity_shares or shares < 1:
                rejections["insufficient_liquidity_or_cash"] += 1
                return False
            if shares < shares_by_size:
                warnings_log.append(f"{s.trading_code}: sized down to {shares} shares on {d.date()} (liquidity/cash limit)")
            notional = shares * exec_price
            fee = notional * self.cost.fee_pct
            slippage_cost = shares * open_px * self.cost.adverse_price_pct
            total_cash_out = notional + fee
            if total_cash_out > cash:
                rejections["insufficient_liquidity_or_cash"] += 1
                return False
            cash -= total_cash_out
            positions[s.stock_id] = _Position(
                stock_id=s.stock_id,
                exchange=s.exchange,
                sector=s.sector,
                entry_date=d,
                entry_signal_date=order.signal_date,
                entry_open_price=open_px,
                entry_exec_price=exec_price,
                shares=shares,
                entry_fee=fee,
                entry_slippage_cost=slippage_cost,
                cash_paid=total_cash_out,
                last_price=float(row["close"]),
                last_price_date=d,
                corp_action_flagged=bool(pd.notna(row.get("return_1d")) and abs(float(row["return_1d"])) * 100 >= CORP_ACTION_RETURN_THRESHOLD_PCT),
            )
            return True

        def close_position(
            pos: _Position,
            d: pd.Timestamp,
            open_px: float,
            *,
            apply_costs: bool,
            reason: str,
            corp_flag: bool,
        ):
            nonlocal cash
            if apply_costs:
                exec_price = open_px * (1 - self.cost.adverse_price_pct)
                notional = pos.shares * exec_price
                fee = notional * self.cost.fee_pct
                exit_slippage_cost = pos.shares * open_px * self.cost.adverse_price_pct
                cash_received = notional - fee
            else:
                # Synthetic end-of-window mark-to-market close: no real
                # order was placed, so no spread/slippage is charged, but a
                # brokerage/tax estimate is still applied since the
                # position would need to be liquidated eventually.
                exec_price = open_px
                notional = pos.shares * exec_price
                fee = notional * self.cost.fee_pct
                exit_slippage_cost = 0.0
                cash_received = notional - fee
            cash += cash_received
            net_pnl = cash_received - pos.cash_paid
            # gross_pnl must use the RAW price on both legs (no spread/
            # slippage/fees) so that net_pnl == gross_pnl - total_costs
            # holds exactly — mixing a post-slippage exec_price with the
            # raw entry_open_price here would double-count the exit-side
            # slippage inside "gross" P&L instead of inside "costs".
            gross_pnl = pos.shares * (open_px - pos.entry_open_price)
            total_costs = pos.entry_fee + pos.entry_slippage_cost + fee + exit_slippage_cost
            trades.append(
                {
                    "stock_id": pos.stock_id,
                    "exchange": pos.exchange,
                    "sector": pos.sector,
                    "signal_date": pos.entry_signal_date.date().isoformat(),
                    "entry_date": pos.entry_date.date().isoformat(),
                    "exit_date": d.date().isoformat(),
                    "entry_open_price": round(pos.entry_open_price, 4),
                    "entry_price": round(pos.entry_exec_price, 4),
                    "exit_price": round(exec_price, 4),
                    "shares": pos.shares,
                    "gross_pnl": round(gross_pnl, 2),
                    "net_pnl": round(net_pnl, 2),
                    "costs": round(total_costs, 2),
                    "return_pct": round((net_pnl / pos.cash_paid * 100), 3) if pos.cash_paid else 0.0,
                    "hold_calendar_days": (d - pos.entry_date).days,
                    "reason": reason,
                    "corp_action_flagged": bool(pos.corp_action_flagged or corp_flag),
                    "year": pos.entry_date.year,
                }
            )

        for d in calendar:
            # 1) Execute (or retry) pending orders scheduled for today.
            still_pending: list[_PendingOrder] = []
            for order in pending:
                s = by_stock[order.stock_id]
                if d < order.target_date:
                    still_pending.append(order)
                    continue
                row = s.frame.loc[d] if d in s.frame.index else None
                if row is None or float(row.get("volume") or 0) <= 0:
                    nxt = self._stock_next_date(s, d)
                    order.attempts_left -= 1
                    if nxt is not None and order.attempts_left > 0:
                        order.target_date = nxt
                        still_pending.append(order)
                    elif order.side == "buy":
                        rejections["entry_abandoned_suspended"] += 1
                        warnings_log.append(
                            f"{s.trading_code}: entry abandoned — no tradable session within "
                            f"{ENTRY_RETRY_SESSIONS} attempts of signal {order.signal_date.date()}"
                        )
                    else:
                        # Can't abandon a scheduled exit — keep retrying;
                        # it will be force-closed in the post-loop sweep if
                        # the stock never trades again before end_date.
                        still_pending.append(order)
                    continue

                if order.side == "buy":
                    open_position(order, s, d, row)
                else:
                    pos = positions.pop(order.stock_id, None)
                    if pos is not None:
                        ret1d = row.get("return_1d")
                        corp_flag = bool(pd.notna(ret1d) and abs(float(ret1d)) * 100 >= CORP_ACTION_RETURN_THRESHOLD_PCT)
                        close_position(pos, d, float(row["open"]), apply_costs=True, reason=order.reason, corp_flag=corp_flag)
            pending = still_pending

            # 2) Exit-trigger detection for open positions with a bar today.
            scheduled_sell_ids = {o.stock_id for o in pending if o.side == "sell"}
            for stock_id, pos in list(positions.items()):
                if stock_id in scheduled_sell_ids:
                    continue
                s = by_stock[stock_id]
                if d not in s.frame.index:
                    continue
                row = s.frame.loc[d]
                close_px = float(row["close"])
                pos.last_price = close_px
                pos.last_price_date = d
                ret1d = row.get("return_1d")
                if pd.notna(ret1d) and abs(float(ret1d)) * 100 >= CORP_ACTION_RETURN_THRESHOLD_PCT:
                    pos.corp_action_flagged = True

                idx_entry = s.date_to_idx.get(pos.entry_date)
                idx_today = s.date_to_idx[d]
                sessions_held = (idx_today - idx_entry) if idx_entry is not None else 0
                target_hit = close_px >= pos.entry_exec_price * (1 + self.pf.target_return_pct / 100.0)
                # Stop triggers from the bar low (not merely its close), then
                # exits at the next executable open, allowing adverse gaps.
                stop_hit = self.pf.stop_loss_pct is not None and float(row.get("low") or close_px) <= pos.entry_exec_price * (1 - self.pf.stop_loss_pct / 100.0)
                time_hit = sessions_held >= self.pf.hold_days
                if target_hit or stop_hit or time_hit:
                    nxt = self._stock_next_date(s, d)
                    reason = "stop_loss" if stop_hit else ("target" if target_hit else "hold_days")
                    if nxt is None:
                        del positions[stock_id]
                        close_position(pos, d, close_px, apply_costs=False, reason="forced_close_no_further_data", corp_flag=pos.corp_action_flagged)
                    else:
                        pending.append(
                            _PendingOrder(stock_id=stock_id, side="sell", signal_date=d, target_date=nxt, attempts_left=10_000, reason=reason)
                        )

            # 3) New-signal detection for stocks not already open/pending.
            open_or_pending_buy = set(positions.keys()) | {o.stock_id for o in pending if o.side == "buy"}
            for s in universe:
                if d not in s.frame.index:
                    continue
                idx = s.date_to_idx[d]
                if idx == 0:
                    continue
                row = s.frame.loc[d]
                prev_row = s.frame.iloc[idx - 1]
                breadth_ok = breadth_by_date.get(d, 0.0) >= self.pf.breadth_threshold
                if not _signal_at(
                    row, prev_row, strategy=self.pf.strategy, breadth_ok=breadth_ok,
                    volume_confirmation_ratio=self.pf.volume_confirmation_ratio,
                ):
                    continue
                if s.stock_id in open_or_pending_buy:
                    rejections["already_open"] += 1
                    continue
                n_open_or_pending = len(positions) + sum(1 for o in pending if o.side == "buy")
                if n_open_or_pending >= self.pf.max_positions:
                    rejections["max_positions"] += 1
                    continue
                nxt = self._stock_next_date(s, d)
                if nxt is None:
                    continue
                pending.append(_PendingOrder(stock_id=s.stock_id, side="buy", signal_date=d, target_date=nxt, attempts_left=ENTRY_RETRY_SESSIONS))
                open_or_pending_buy.add(s.stock_id)

            # 4) Mark-to-market equity for the day.
            positions_value = sum(p.shares * p.last_price for p in positions.values())
            equity = cash + positions_value
            equity_curve.append(
                {
                    "date": d,
                    "equity": equity,
                    "cash": cash,
                    "positions_value": positions_value,
                    "n_positions": len(positions),
                    "exposure_pct": (positions_value / equity * 100.0) if equity else 0.0,
                }
            )

        # Force-close anything still open (or still pending a sell that
        # never found a further session) at its last known
        # mark-to-market price — flagged, never silently dropped.
        for order in [o for o in pending if o.side == "sell"]:
            pos = positions.pop(order.stock_id, None)
            if pos is not None:
                close_position(pos, pos.last_price_date, pos.last_price, apply_costs=False, reason="forced_close_at_backtest_end", corp_flag=pos.corp_action_flagged)
        for stock_id, pos in list(positions.items()):
            close_position(pos, pos.last_price_date, pos.last_price, apply_costs=False, reason="forced_close_at_backtest_end", corp_flag=pos.corp_action_flagged)
        positions.clear()

        forced_close_count = sum(1 for t in trades if t["reason"].startswith("forced_close"))
        if forced_close_count:
            warnings_log.append(f"{forced_close_count} position(s) were still open at the end of the window and were force-closed at their last known price (not a real sale).")

        return {
            "ok": True,
            "trades": trades,
            "equity_curve": equity_curve,
            "universe": universe,
            "data_start_date": data_start_date,
            "data_end_date": data_end_date,
            "initial_cash": self.pf.initial_cash,
            "final_cash": cash,
            "rejections": rejections,
            "warnings": warnings_log[:200],
            "stocks_tested": len(universe),
        }


def _compute_metrics(equity_curve: list[dict], trades: list[dict], initial_cash: float) -> dict:
    if not equity_curve:
        return {}
    eq = pd.Series([e["equity"] for e in equity_curve], index=[e["date"] for e in equity_curve])
    final_equity = float(eq.iloc[-1])
    total_return_pct = (final_equity / initial_cash - 1) * 100 if initial_cash else None

    daily_ret = eq.pct_change().dropna()
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    annualized_return_pct = ((final_equity / initial_cash) ** (1 / years) - 1) * 100 if initial_cash and final_equity > 0 else None

    running_max = eq.cummax()
    drawdown = (eq / running_max - 1) * 100
    max_drawdown_pct = float(drawdown.min()) if len(drawdown) else None

    sharpe = sortino = None
    if len(daily_ret) > 2 and daily_ret.std(ddof=0) > 1e-12:
        sharpe = float(daily_ret.mean() / daily_ret.std(ddof=0) * np.sqrt(TRADING_DAYS_PER_YEAR))
        downside = daily_ret[daily_ret < 0]
        if len(downside) > 0 and downside.std(ddof=0) > 1e-12:
            sortino = float(daily_ret.mean() / downside.std(ddof=0) * np.sqrt(TRADING_DAYS_PER_YEAR))

    exposure_pct = float(np.mean([e["exposure_pct"] for e in equity_curve]))

    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] <= 0]
    win_rate = len(wins) / len(trades) if trades else 0.0
    avg_win_pct = float(np.mean([t["return_pct"] for t in wins])) if wins else None
    avg_loss_pct = float(np.mean([t["return_pct"] for t in losses])) if losses else None
    gross_profit = sum(t["net_pnl"] for t in wins)
    gross_loss = abs(sum(t["net_pnl"] for t in losses))
    profit_factor = round(gross_profit / gross_loss, 3) if gross_loss > 1e-9 else None
    total_costs = float(sum(t["costs"] for t in trades))
    total_traded_notional = float(sum(t["shares"] * t["entry_price"] + t["shares"] * t["exit_price"] for t in trades))
    turnover_pct = (total_traded_notional / initial_cash * 100) if initial_cash else None
    avg_return_pct = float(np.mean([t["return_pct"] for t in trades])) if trades else 0.0

    return {
        "final_equity": round(final_equity, 2),
        "total_return_pct": round(total_return_pct, 3) if total_return_pct is not None else None,
        "annualized_return_pct": round(annualized_return_pct, 3) if annualized_return_pct is not None else None,
        "max_drawdown_pct": round(max_drawdown_pct, 3) if max_drawdown_pct is not None else None,
        "sharpe_ratio": round(sharpe, 3) if sharpe is not None else None,
        "sortino_ratio": round(sortino, 3) if sortino is not None else None,
        "profit_factor": profit_factor,
        "win_rate": round(win_rate, 4),
        "avg_return_pct": round(avg_return_pct, 3),
        "avg_win_pct": round(avg_win_pct, 3) if avg_win_pct is not None else None,
        "avg_loss_pct": round(avg_loss_pct, 3) if avg_loss_pct is not None else None,
        "turnover_pct": round(turnover_pct, 3) if turnover_pct is not None else None,
        "total_costs": round(total_costs, 2),
        "avg_exposure_pct": round(exposure_pct, 3) if exposure_pct is not None else None,
        "total_trades": len(trades),
    }


def _breakdown_by(trades: list[dict], key: str) -> dict:
    groups: dict[str, list[dict]] = {}
    for t in trades:
        groups.setdefault(str(t[key]), []).append(t)
    out = {}
    for k, ts in sorted(groups.items()):
        rets = [t["return_pct"] for t in ts]
        wins = sum(1 for t in ts if t["net_pnl"] > 0)
        entry = {
            "trades": len(ts),
            "win_rate": round(wins / len(ts), 4) if ts else 0.0,
            "avg_return_pct": round(float(np.mean(rets)), 3) if rets else 0.0,
            "total_net_pnl": round(float(sum(t["net_pnl"] for t in ts)), 2),
        }
        if len(ts) < MIN_SAMPLE_FOR_BREAKDOWN:
            entry["warning"] = f"small sample (n={len(ts)} < {MIN_SAMPLE_FOR_BREAKDOWN}); interpret cautiously"
        out[k] = entry
    return out


def _breakdown_all(trades: list[dict]) -> dict:
    return {
        "by_year": _breakdown_by(trades, "year"),
        "by_exchange": _breakdown_by(trades, "exchange"),
        "by_sector": _breakdown_by(trades, "sector"),
    }


def _buy_and_hold_return_pct(universe: list[StockSeries]) -> float | None:
    """Equal-weight basket of the same stocks the strategy actually
    considered, bought at each stock's first in-window open and held to
    its last in-window close (a stock that stops trading mid-window is
    held only to its own last observation, not force-marked further)."""
    rets = []
    for s in universe:
        if not s.window_dates:
            continue
        first_d, last_d = s.window_dates[0], s.window_dates[-1]
        open_px = float(s.frame.loc[first_d, "open"])
        close_px = float(s.frame.loc[last_d, "close"])
        if open_px > 0:
            rets.append(close_px / open_px - 1)
    if not rets:
        return None
    return round(float(np.mean(rets)) * 100, 3)


def _exchange_index_proxy_return_pct(exchange: str | None, start_date: date, end_date: date) -> float | None:
    """No official DSEX/CSCX index level is stored in this database
    (MarketSnapshot has only a handful of rows). This is an equal-weight
    proxy built from the same daily stock returns already used for
    close-learn's cross-sectional context — not the official index."""
    if not exchange:
        return None
    from market.services.close_learn import build_exchange_context

    ctx = build_exchange_context(exchange, start=start_date, end=end_date)
    if ctx.empty:
        return None
    ctx = ctx[(ctx["date"] >= pd.Timestamp(start_date)) & (ctx["date"] <= pd.Timestamp(end_date))]
    if ctx.empty:
        return None
    # build_exchange_context() divides by the prior day's close per stock;
    # a stray zero/near-zero close in PriceHistory (a known real data-
    # quality gap — see the Phase 3 note on PriceHistory having no value
    # validator) can produce an inf/nan daily return that would otherwise
    # blow up the whole compounded proxy. Drop non-finite days and clip to
    # a generous +/-50%/day band (BD circuit breakers are far tighter)
    # rather than let one bad data point invalidate the benchmark.
    rets = ctx["index_ret_1d"].replace([np.inf, -np.inf], np.nan).dropna()
    if rets.empty:
        return None
    rets = rets.clip(-0.5, 0.5)
    cum = float((1 + rets).prod() - 1)
    if not np.isfinite(cum):
        return None
    return round(cum * 100, 3)
