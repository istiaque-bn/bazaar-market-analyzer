from decimal import Decimal

from django.db import models
from django.contrib.auth.models import User


class Exchange(models.TextChoices):
    DSE = "DSE", "Dhaka Stock Exchange"
    CSE = "CSE", "Chittagong Stock Exchange"


class StockGroup(models.TextChoices):
    A = "A", "A"
    B = "B", "B"
    G = "G", "G"
    N = "N", "N"
    Z = "Z", "Z"
    UNKNOWN = "U", "Unknown"


class SignalAction(models.TextChoices):
    BUY = "BUY", "Buy"
    HOLD = "HOLD", "Hold"
    SELL = "SELL", "Sell"
    WATCH = "WATCH", "Watch"


class DataSource(models.TextChoices):
    """Where a PriceHistory row actually came from. UNKNOWN is the default
    for every pre-Phase-6 row — provenance was never tracked before, and
    there is no reliable way to reconstruct it after the fact (see the
    Phase 6 report), so legacy rows are honestly labeled unknown rather
    than guessed as live."""

    UNKNOWN = "unknown", "Unknown (legacy / unspecified)"
    DSE_LIVE = "dse_live", "DSE live quote"
    DSE_HISTORY = "dse_history", "DSE historical archive/bdshare"
    DSE_RESEARCH_BACKFILL = "dse_research_backfill", "DSE external research backfill (CC BY 4.0)"
    CSE_LIVE = "cse_live", "CSE live quote"
    CSE_HISTORY = "cse_history", "CSE historical bulk download"
    CSE_MIRROR_FALLBACK = "cse_mirror_fallback", "CSE mirrored from DSE (not real CSE data)"
    SYNTHETIC_DEMO = "synthetic_demo", "Synthetic demo seed data"
    SYNTHETIC_FALLBACK = "synthetic_fallback", "Synthetic fallback for a real code (fetch unavailable)"


SYNTHETIC_SOURCES = {
    DataSource.CSE_MIRROR_FALLBACK,
    DataSource.SYNTHETIC_DEMO,
    DataSource.SYNTHETIC_FALLBACK,
}


class AdjustmentStatus(models.TextChoices):
    UNKNOWN = "unknown", "Unknown"
    RAW = "raw", "Raw / as-received (no corporate-action adjustment applied)"
    ADJUSTED = "adjusted", "Adjusted for corporate actions"


class ImportBatch(models.Model):
    """One row per fetch/seed operation. Groups the PriceHistory rows it
    wrote and keeps bounded, secret-free metadata for troubleshooting —
    never raw auth headers/cookies/tokens, and never an unbounded raw
    response body."""

    source = models.CharField(max_length=32, choices=DataSource.choices, default=DataSource.UNKNOWN, db_index=True)
    exchange = models.CharField(max_length=3, choices=Exchange.choices, blank=True)
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    row_count = models.IntegerField(default=0)
    stock_count = models.IntegerField(default=0)
    request_meta = models.JSONField(default=dict, blank=True, help_text="URL/sub-source/status/counts/timing only — never headers, cookies or tokens")
    error = models.TextField(blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.source} batch #{self.id} ({self.row_count} rows)"


class Stock(models.Model):
    exchange = models.CharField(max_length=3, choices=Exchange.choices, default=Exchange.DSE)
    trading_code = models.CharField(max_length=32, db_index=True)
    company_name = models.CharField(max_length=255, blank=True)
    sector = models.CharField(max_length=128, blank=True)
    group = models.CharField(max_length=1, choices=StockGroup.choices, default=StockGroup.UNKNOWN)
    is_active = models.BooleanField(default=True)
    pe_ratio = models.FloatField(null=True, blank=True)
    eps = models.FloatField(null=True, blank=True)
    last_price = models.FloatField(null=True, blank=True)
    last_change_pct = models.FloatField(null=True, blank=True)
    last_volume = models.BigIntegerField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("exchange", "trading_code")
        ordering = ["trading_code"]

    def __str__(self):
        return f"{self.trading_code} ({self.exchange})"

    @property
    def slug(self):
        return f"{self.exchange}-{self.trading_code}".lower()


class PriceHistoryQuerySet(models.QuerySet):
    def live(self):
        """Excludes synthetic/demo/mirror-fallback rows — the default for
        analysis, training and backtests. Legacy rows with unknown
        provenance are NOT excluded here: treating the entire pre-Phase-6
        dataset as suspect would make the product unusable, and "unknown"
        is not evidence of being fake. See the Phase 6 report for the
        honest count of unknown-provenance rows still in this queryset."""
        return self.exclude(is_synthetic=True)

    def demo_only(self):
        return self.filter(is_synthetic=True)


class PriceHistoryManager(models.Manager.from_queryset(PriceHistoryQuerySet)):
    pass


class PriceHistory(models.Model):
    stock = models.ForeignKey(Stock, on_delete=models.CASCADE, related_name="prices")
    date = models.DateField(db_index=True)
    open = models.FloatField()
    high = models.FloatField()
    low = models.FloatField()
    close = models.FloatField()
    volume = models.BigIntegerField(default=0)
    value = models.FloatField(default=0)

    # --- Phase 6: provenance & quality -----------------------------------
    source = models.CharField(max_length=32, choices=DataSource.choices, default=DataSource.UNKNOWN, db_index=True)
    is_synthetic = models.BooleanField(default=False, db_index=True, help_text="True for demo-seeded/mirror-fallback data — excluded from live analysis by default")
    fetched_at = models.DateTimeField(null=True, blank=True, help_text="When this row was actually written by a fetch/seed; null for legacy rows where this was never recorded")
    adjustment_status = models.CharField(max_length=16, choices=AdjustmentStatus.choices, default=AdjustmentStatus.UNKNOWN)
    import_batch = models.ForeignKey(ImportBatch, null=True, blank=True, on_delete=models.SET_NULL, related_name="price_rows")
    quality_flags = models.JSONField(default=list, blank=True, help_text="Detected issues: impossible OHLC, abnormal jump, stale-quote run, missing-session gap, etc.")

    objects = PriceHistoryManager()

    class Meta:
        unique_together = ("stock", "date")
        ordering = ["-date"]
        verbose_name_plural = "price histories"
        indexes = [
            models.Index(fields=["stock", "-date"]),
        ]

    def __str__(self):
        return f"{self.stock.trading_code} {self.date} {self.close}"


class PriceHistoryRevision(models.Model):
    """Audit trail: a snapshot of a PriceHistory row's prior values,
    written whenever a write path is about to materially change an
    existing row (e.g. a later fetch overwrites an earlier one for the
    same stock/date) — so that overwrite is never a silent loss of
    evidence. price_history uses SET_NULL (not CASCADE) so this survives
    even if the row it describes is ever deleted."""

    price_history = models.ForeignKey(PriceHistory, null=True, blank=True, on_delete=models.SET_NULL, related_name="revisions")
    stock = models.ForeignKey(Stock, on_delete=models.CASCADE, related_name="price_revisions")
    date = models.DateField(db_index=True)
    previous_open = models.FloatField(null=True, blank=True)
    previous_high = models.FloatField(null=True, blank=True)
    previous_low = models.FloatField(null=True, blank=True)
    previous_close = models.FloatField(null=True, blank=True)
    previous_volume = models.BigIntegerField(null=True, blank=True)
    previous_source = models.CharField(max_length=32, blank=True)
    new_source = models.CharField(max_length=32, blank=True)
    reason = models.CharField(max_length=64, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-recorded_at"]

    def __str__(self):
        return f"{self.stock.trading_code} {self.date} revision @ {self.recorded_at:%Y-%m-%d %H:%M}"


class TechnicalSnapshot(models.Model):
    stock = models.ForeignKey(Stock, on_delete=models.CASCADE, related_name="technicals")
    as_of = models.DateField(db_index=True)
    sma_20 = models.FloatField(null=True, blank=True)
    sma_50 = models.FloatField(null=True, blank=True)
    sma_200 = models.FloatField(null=True, blank=True)
    ema_12 = models.FloatField(null=True, blank=True)
    ema_26 = models.FloatField(null=True, blank=True)
    rsi_14 = models.FloatField(null=True, blank=True)
    macd = models.FloatField(null=True, blank=True)
    macd_signal = models.FloatField(null=True, blank=True)
    macd_hist = models.FloatField(null=True, blank=True)
    bb_upper = models.FloatField(null=True, blank=True)
    bb_middle = models.FloatField(null=True, blank=True)
    bb_lower = models.FloatField(null=True, blank=True)
    atr_14 = models.FloatField(null=True, blank=True)
    volume_sma_20 = models.FloatField(null=True, blank=True)
    support = models.FloatField(null=True, blank=True)
    resistance = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("stock", "as_of")
        ordering = ["-as_of"]

    def __str__(self):
        return f"Tech {self.stock.trading_code} {self.as_of}"


class PatternHit(models.Model):
    stock = models.ForeignKey(Stock, on_delete=models.CASCADE, related_name="patterns")
    as_of = models.DateField(db_index=True)
    name = models.CharField(max_length=64)
    direction = models.CharField(max_length=8)  # bullish / bearish / neutral
    strength = models.FloatField(default=0.5)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-as_of", "-strength"]

    def __str__(self):
        return f"{self.name} on {self.stock.trading_code}"


class AnalysisResult(models.Model):
    stock = models.ForeignKey(Stock, on_delete=models.CASCADE, related_name="analyses")
    as_of = models.DateField(db_index=True)
    action = models.CharField(max_length=8, choices=SignalAction.choices, default=SignalAction.HOLD)
    score = models.FloatField(default=0)  # -100 to +100
    confidence = models.FloatField(default=0)  # 0 to 1
    risk_level = models.CharField(max_length=16, default="medium")  # low/medium/high
    is_safe_buy = models.BooleanField(default=False)
    maturity_days_est = models.IntegerField(null=True, blank=True)
    peak_days_est = models.IntegerField(null=True, blank=True)
    expected_return_pct = models.FloatField(null=True, blank=True)
    probability = models.FloatField(null=True, blank=True)
    rationale = models.TextField(blank=True)
    features = models.JSONField(default=dict, blank=True)
    ml_score = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("stock", "as_of")
        ordering = ["-score", "-confidence"]

    def __str__(self):
        return f"{self.stock.trading_code} {self.action} ({self.score:.0f})"

    @property
    def expected_peak_price(self) -> float | None:
        """Last price × (1 + expected peak return%)."""
        price = self.stock.last_price
        if price is None or self.expected_return_pct is None:
            return None
        return round(float(price) * (1 + float(self.expected_return_pct) / 100.0), 2)


class PaperTradingAccount(models.Model):
    """Singleton-like autonomous virtual account. It never connects to a broker."""

    name = models.CharField(max_length=64, unique=True, default="Admin autonomous paper fund")
    initial_cash = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("100000.00"))
    cash = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("100000.00"))
    is_active = models.BooleanField(default=True, db_index=True)
    strategy_config = models.JSONField(default=dict, blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.name


class PaperPosition(models.Model):
    account = models.ForeignKey(PaperTradingAccount, on_delete=models.CASCADE, related_name="positions")
    stock = models.ForeignKey(Stock, on_delete=models.PROTECT, related_name="paper_positions")
    quantity = models.PositiveIntegerField()
    entry_price = models.DecimalField(max_digits=14, decimal_places=4)
    entry_fee = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))
    opened_on = models.DateField(db_index=True)
    maturity_date = models.DateField(null=True, blank=True, db_index=True, help_text="First session on which the virtual shares may be sold")
    signal = models.ForeignKey(AnalysisResult, null=True, blank=True, on_delete=models.SET_NULL, related_name="paper_entries")
    is_open = models.BooleanField(default=True, db_index=True)
    closed_on = models.DateField(null=True, blank=True, db_index=True)
    exit_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    exit_fee = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))
    realized_pnl = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    exit_reason = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-opened_on", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["account", "stock"], condition=models.Q(is_open=True), name="one_open_paper_position_per_stock"),
        ]

    def __str__(self):
        return f"{self.stock} x {self.quantity} ({'open' if self.is_open else 'closed'})"


class PaperTrade(models.Model):
    class Side(models.TextChoices):
        BUY = "BUY", "Buy"
        SELL = "SELL", "Sell"

    account = models.ForeignKey(PaperTradingAccount, on_delete=models.CASCADE, related_name="trades")
    position = models.ForeignKey(PaperPosition, on_delete=models.CASCADE, related_name="trades")
    stock = models.ForeignKey(Stock, on_delete=models.PROTECT, related_name="paper_trades")
    side = models.CharField(max_length=4, choices=Side.choices)
    trade_date = models.DateField(db_index=True)
    settlement_date = models.DateField(null=True, blank=True, db_index=True)
    quantity = models.PositiveIntegerField()
    market_price = models.DecimalField(max_digits=14, decimal_places=4)
    execution_price = models.DecimalField(max_digits=14, decimal_places=4)
    fee = models.DecimalField(max_digits=14, decimal_places=2)
    cash_effect = models.DecimalField(max_digits=14, decimal_places=2, help_text="Negative for buys, positive for sells")
    reason = models.CharField(max_length=128)
    signal = models.ForeignKey(AnalysisResult, null=True, blank=True, on_delete=models.SET_NULL, related_name="paper_trades")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-trade_date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["position", "side"], name="one_paper_trade_per_position_side"),
        ]


class PaperLearningFeedback(models.Model):
    """Settled, immutable outcome for a prediction acted on by paper trading."""

    position = models.OneToOneField(PaperPosition, on_delete=models.CASCADE, related_name="learning_feedback")
    stock = models.ForeignKey(Stock, on_delete=models.PROTECT, related_name="paper_learning_feedback")
    signal_date = models.DateField(db_index=True)
    outcome_date = models.DateField(db_index=True)
    predicted_probability = models.FloatField(null=True, blank=True)
    predicted_confidence = models.FloatField(null=True, blank=True)
    predicted_score = models.FloatField(null=True, blank=True)
    gross_return_pct = models.FloatField()
    net_return_pct = models.FloatField()
    profitable_after_costs = models.BooleanField(db_index=True)
    holding_sessions = models.PositiveIntegerField()
    exit_reason = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-outcome_date", "-id"]

    def __str__(self):
        return f"{self.stock} {self.signal_date}: {self.net_return_pct:+.2f}%"


class PaperCashSettlement(models.Model):
    """Virtual sale proceeds that cannot be reused until exchange settlement."""

    account = models.ForeignKey(PaperTradingAccount, on_delete=models.CASCADE, related_name="cash_settlements")
    trade = models.OneToOneField(PaperTrade, on_delete=models.CASCADE, related_name="cash_settlement")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    settlement_date = models.DateField(db_index=True)
    is_settled = models.BooleanField(default=False, db_index=True)
    settled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["settlement_date", "id"]


class PaperEquitySnapshot(models.Model):
    account = models.ForeignKey(PaperTradingAccount, on_delete=models.CASCADE, related_name="equity_snapshots")
    as_of = models.DateField(db_index=True)
    cash = models.DecimalField(max_digits=14, decimal_places=2)
    holdings_value = models.DecimalField(max_digits=14, decimal_places=2)
    total_equity = models.DecimalField(max_digits=14, decimal_places=2)
    realized_pnl = models.DecimalField(max_digits=14, decimal_places=2)
    unrealized_pnl = models.DecimalField(max_digits=14, decimal_places=2)
    open_positions = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-as_of"]
        constraints = [models.UniqueConstraint(fields=["account", "as_of"], name="one_paper_equity_snapshot_per_day")]


class BacktestRun(models.Model):
    name = models.CharField(max_length=128)
    strategy = models.CharField(max_length=64)
    exchange = models.CharField(max_length=3, choices=Exchange.choices, blank=True)
    # start_date/end_date are the REQUESTED window, strictly enforced (no
    # signal/execution outside it). data_start_date/data_end_date are the
    # actual first/last dates among observations truly used, which can be
    # narrower than requested if a stock's history doesn't reach that far —
    # never wider.
    start_date = models.DateField()
    end_date = models.DateField()
    data_start_date = models.DateField(null=True, blank=True)
    data_end_date = models.DateField(null=True, blank=True)

    engine_version = models.CharField(
        max_length=8,
        default="v1",
        help_text="v1 = pre-Phase-5 single-stock engine (legacy rows, kept for history). v2 = portfolio engine with costs/liquidity/benchmarks.",
    )

    total_trades = models.IntegerField(default=0)
    win_rate = models.FloatField(default=0)
    avg_return_pct = models.FloatField(default=0)
    avg_win_pct = models.FloatField(null=True, blank=True)
    avg_loss_pct = models.FloatField(null=True, blank=True)
    avg_days_to_peak = models.FloatField(null=True, blank=True)
    avg_days_to_target = models.FloatField(null=True, blank=True)
    max_drawdown_pct = models.FloatField(null=True, blank=True)

    initial_cash = models.FloatField(null=True, blank=True)
    final_cash = models.FloatField(null=True, blank=True)
    final_equity = models.FloatField(null=True, blank=True)
    total_return_pct = models.FloatField(null=True, blank=True)
    annualized_return_pct = models.FloatField(null=True, blank=True)
    sharpe_ratio = models.FloatField(null=True, blank=True)
    sortino_ratio = models.FloatField(null=True, blank=True)
    profit_factor = models.FloatField(null=True, blank=True)
    turnover_pct = models.FloatField(null=True, blank=True)
    total_costs = models.FloatField(null=True, blank=True)
    avg_exposure_pct = models.FloatField(null=True, blank=True)

    benchmark_buy_hold_return_pct = models.FloatField(null=True, blank=True)
    benchmark_index_return_pct = models.FloatField(null=True, blank=True)

    cost_config = models.JSONField(default=dict, blank=True)
    breakdown = models.JSONField(default=dict, blank=True, help_text="Results by year / exchange / sector, with min-sample warnings")
    warnings = models.JSONField(default=list, blank=True, help_text="Suspended securities, corporate-action exclusions, rejected/sized-down trades, etc.")
    summary = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.win_rate:.0%} win)"


class MarketSnapshot(models.Model):
    exchange = models.CharField(max_length=3, choices=Exchange.choices)
    as_of = models.DateField()
    index_value = models.FloatField(null=True, blank=True)
    index_change_pct = models.FloatField(null=True, blank=True)
    total_volume = models.BigIntegerField(null=True, blank=True)
    total_value = models.FloatField(null=True, blank=True)
    advancers = models.IntegerField(default=0)
    decliners = models.IntegerField(default=0)
    unchanged = models.IntegerField(default=0)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("exchange", "as_of")
        ordering = ["-as_of"]

    def __str__(self):
        return f"{self.exchange} {self.as_of}"


class MarketHoliday(models.Model):
    """A day both DSE and CSE were closed for a reason other than the
    regular Sun-Thu trading week (national holidays, Eid, etc.). There's no
    official machine-readable DSE/CSE holiday calendar to sync from, so this
    is maintained by hand (via /admin) — see market/services/trading_calendar.py,
    which is the only reader of this table."""

    date = models.DateField(unique=True)
    name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date"]

    def __str__(self):
        return f"{self.date} - {self.name}"


class Watchlist(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="watchlists")
    name = models.CharField(max_length=64, default="Default")
    stocks = models.ManyToManyField(Stock, blank=True, related_name="watchlists")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "name")

    def __str__(self):
        return f"{self.user.username}: {self.name}"


class NextDayCloseForecast(models.Model):
    """After-close forecast of the next trading day's closing price (learn loop)."""

    stock = models.ForeignKey(Stock, on_delete=models.CASCADE, related_name="close_forecasts")
    as_of = models.DateField(db_index=True, help_text="Session date when the forecast was made (after close)")
    target_date = models.DateField(db_index=True, help_text="Next trading day being forecast")
    last_close = models.FloatField()
    predicted_close = models.FloatField()
    predicted_return = models.FloatField(help_text="Predicted close/close−1 return")
    confidence = models.FloatField(default=0.5)
    method = models.CharField(max_length=32, default="analogue+bias")
    features = models.JSONField(default=dict, blank=True)
    actual_close = models.FloatField(null=True, blank=True)
    abs_error = models.FloatField(null=True, blank=True)
    pct_error = models.FloatField(null=True, blank=True)
    return_error = models.FloatField(null=True, blank=True, help_text="predicted_return − actual_return")
    settled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("stock", "target_date")
        ordering = ["-target_date", "-as_of"]
        indexes = [
            models.Index(fields=["settled_at"]),
            models.Index(fields=["as_of"]),
        ]

    def __str__(self):
        return f"{self.stock.trading_code} → {self.target_date} pred={self.predicted_close}"


class CloseLearnState(models.Model):
    """Running correction learned from settled next-day close forecasts."""

    key = models.CharField(max_length=32, unique=True, default="global")
    return_bias = models.FloatField(
        default=0.0,
        help_text="EMA of (predicted_return − actual_return); subtracted from future forecasts",
    )
    mae = models.FloatField(default=0.0, help_text="Mean absolute price error on settled forecasts")
    mape = models.FloatField(default=0.0, help_text="Mean abs % error")
    direction_hit_rate = models.FloatField(default=0.0)
    settled_count = models.IntegerField(default=0)
    last_forecast_at = models.DateTimeField(null=True, blank=True)
    last_settled_at = models.DateTimeField(null=True, blank=True)
    last_trained_at = models.DateTimeField(null=True, blank=True)
    extras = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"CloseLearn[{self.key}] bias={self.return_bias:.4f} n={self.settled_count}"


class MLModelStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    EXPERIMENTAL = "experimental", "Experimental"
    INACTIVE = "inactive", "Inactive"


class MLModelVersion(models.Model):
    """One row per training run of a given model family. Only a training
    run whose walk-forward out-of-sample skill vs. its naive baseline is
    positive may be flagged is_active — inference code must refuse to use
    experimental/inactive models for confident (score-shifting) output."""

    model_name = models.CharField(max_length=64, db_index=True, help_text="e.g. forward_return_rf, next_close_rf")
    version = models.CharField(max_length=32, help_text="Timestamp-based, e.g. 20260729-101530")
    exchange_scope = models.CharField(max_length=16, default="combined", help_text="DSE / CSE / combined")
    status = models.CharField(max_length=16, choices=MLModelStatus.choices, default=MLModelStatus.EXPERIMENTAL)
    is_active = models.BooleanField(default=False, db_index=True)
    data_cutoff = models.DateField(help_text="Latest price bar date included in training data")
    feature_schema = models.JSONField(default=list, blank=True)
    train_rows = models.IntegerField(default=0)
    fold_metadata = models.JSONField(default=list, blank=True, help_text="Per-fold dates, sample counts, class balance")
    metrics = models.JSONField(default=dict, blank=True, help_text="Aggregated walk-forward metrics incl. baseline comparisons")
    file_path = models.CharField(max_length=255, blank=True)
    backup_path = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)
    trained_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-trained_at"]

    def __str__(self):
        return f"{self.model_name}[{self.exchange_scope}] {self.version} ({self.status})"


class TaskStatus(models.TextChoices):
    STARTED = "started", "Started"
    SUCCESS = "success", "Success"
    PARTIAL = "partial", "Partial success"
    SKIPPED = "skipped", "Skipped"
    FAILURE = "failure", "Failure"


class TaskRun(models.Model):
    """One row per background-task execution attempt — gives an
    inspectable, persistent success/failure record independent of the
    Celery result backend (which is opaque Redis state)."""

    task_name = models.CharField(max_length=128, db_index=True)
    status = models.CharField(max_length=16, choices=TaskStatus.choices, default=TaskStatus.STARTED)
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.task_name} [{self.status}] {self.started_at:%Y-%m-%d %H:%M}"


class TaskHealthStatus(models.TextChoices):
    HEALTHY = "healthy", "Healthy"
    UNHEALTHY = "unhealthy", "Unhealthy"


class TaskAlertState(models.TextChoices):
    NONE = "none", "No pending alert"
    FAILURE_PENDING = "failure_pending", "Failure alert pending"
    FAILURE_SENT = "failure_sent", "Failure alert sent"
    RECOVERY_PENDING = "recovery_pending", "Recovery alert pending"


class TaskHealth(models.Model):
    """Current, stateful health of a task; TaskRun remains the immutable audit log."""
    task_name = models.CharField(max_length=128, unique=True)
    last_run = models.DateTimeField(null=True, blank=True)
    last_success = models.DateTimeField(null=True, blank=True)
    last_failure = models.DateTimeField(null=True, blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    current_health_status = models.CharField(max_length=16, choices=TaskHealthStatus.choices, default=TaskHealthStatus.HEALTHY)
    last_error = models.TextField(blank=True)
    run_duration_seconds = models.FloatField(null=True, blank=True)
    symbols_attempted = models.PositiveIntegerField(default=0)
    symbols_successful = models.PositiveIntegerField(default=0)
    symbols_failed = models.PositiveIntegerField(default=0)
    alert_state = models.CharField(max_length=24, choices=TaskAlertState.choices, default=TaskAlertState.NONE)
    last_alert = models.DateTimeField(null=True, blank=True)
    last_recovery = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.task_name} [{self.current_health_status}]"


class PredictionSnapshot(models.Model):
    """One immutable row per served prediction from either ML model
    family, captured from that day's existing prediction artifact
    (AnalysisResult.ml_score for forward_return_rf, NextDayCloseForecast
    for next_close_rf) rather than by re-running inference — see
    market.services.reliability_capture. Once written, the predicted_*/
    *_baseline_*/reference_close/confidence_* fields are never modified;
    only settlement (outcome_*/settlement_*) writes to a row after
    creation, and only while it's still PENDING. This is what makes the
    row a trustworthy point-in-time record of what was actually
    predicted, immune to later feature/price corrections."""

    class ModelFamily(models.TextChoices):
        FORWARD_RETURN_RF = "forward_return_rf", "Forward Return Classifier"
        NEXT_CLOSE_RF = "next_close_rf", "Next Close Regressor"

    class SettlementStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        SETTLED = "settled", "Settled"
        EXCLUDED = "excluded", "Excluded"

    model_family = models.CharField(max_length=32, choices=ModelFamily.choices, db_index=True)
    model_version = models.ForeignKey(
        MLModelVersion, null=True, blank=True, on_delete=models.SET_NULL, related_name="prediction_snapshots"
    )
    model_version_tag = models.CharField(max_length=32, help_text="Immutable copy of MLModelVersion.version at capture time")
    feature_schema_version = models.CharField(max_length=32, blank=True, help_text="Hash of the feature-column list used at capture time")

    exchange = models.CharField(max_length=3, choices=Exchange.choices, db_index=True)
    stock = models.ForeignKey(Stock, null=True, blank=True, on_delete=models.SET_NULL, related_name="prediction_snapshots")
    stock_trading_code = models.CharField(max_length=32, help_text="Immutable copy of the stock's trading code at capture time")

    prediction_at = models.DateTimeField(auto_now_add=True, db_index=True)
    data_cutoff_date = models.DateField(db_index=True, help_text="Last trading session whose price data fed this prediction")
    horizon_trading_days = models.PositiveSmallIntegerField()
    target_date = models.DateField(null=True, blank=True, db_index=True, help_text="Trading session this prediction is settled against")

    reference_close = models.FloatField(help_text="Close price at data_cutoff_date — the entry price for economic evaluation")

    # Classifier-only (forward_return_rf)
    predicted_class = models.BooleanField(null=True, blank=True, help_text="True = forward return predicted positive")
    predicted_probability = models.FloatField(null=True, blank=True)
    rule_baseline_class = models.BooleanField(null=True, blank=True, help_text="predictor.predict_stock's rule-score sign")
    naive_baseline_class = models.BooleanField(null=True, blank=True, help_text="Majority class observed in the model's own training data")

    # Regressor-only (next_close_rf)
    predicted_return = models.FloatField(null=True, blank=True)
    predicted_price = models.FloatField(null=True, blank=True)
    rule_baseline_return = models.FloatField(null=True, blank=True, help_text="Persistence baseline: prior session's 1-day return")
    naive_baseline_return = models.FloatField(null=True, blank=True, help_text="Zero-return (no-change) baseline")

    confidence_value = models.FloatField(null=True, blank=True)
    confidence_label = models.CharField(max_length=16, blank=True)

    data_quality_status = models.CharField(max_length=16, default="ok", help_text="ok / stale / thin_liquidity")
    data_quality_notes = models.JSONField(default=dict, blank=True)

    outcome_return = models.FloatField(null=True, blank=True)
    outcome_class = models.BooleanField(null=True, blank=True)
    outcome_price = models.FloatField(null=True, blank=True)

    settlement_status = models.CharField(
        max_length=16, choices=SettlementStatus.choices, default=SettlementStatus.PENDING, db_index=True
    )
    settled_at = models.DateTimeField(null=True, blank=True)
    exclusion_reason = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["-prediction_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["model_family", "model_version_tag", "stock_trading_code", "exchange", "data_cutoff_date", "horizon_trading_days"],
                name="uniq_prediction_snapshot",
            )
        ]
        indexes = [
            models.Index(fields=["model_family", "exchange", "settlement_status"]),
            models.Index(fields=["model_family", "exchange", "horizon_trading_days", "target_date"]),
        ]

    def __str__(self):
        return f"{self.model_family}[{self.exchange}] {self.stock_trading_code} @ {self.data_cutoff_date} ({self.settlement_status})"


class ReliabilityAssessment(models.Model):
    """One immutable row per (model family, exchange, horizon, window)
    per assessment run — see market.services.reliability_report. Never
    updated after creation; history accumulates as new rows, so past
    assessments can't be silently revised."""

    class Status(models.TextChoices):
        INSUFFICIENT_DATA = "insufficient_data", "Insufficient data"
        HEALTHY = "healthy", "Healthy"
        WATCH = "watch", "Watch"
        DEGRADED = "degraded", "Degraded"
        CRITICAL = "critical", "Critical"

    run_at = models.DateTimeField(auto_now_add=True, db_index=True)
    schema_version = models.CharField(max_length=16, default="1")

    model_family = models.CharField(max_length=32, choices=PredictionSnapshot.ModelFamily.choices, db_index=True)
    model_version = models.ForeignKey(
        MLModelVersion, null=True, blank=True, on_delete=models.SET_NULL, related_name="reliability_assessments"
    )
    model_version_tag = models.CharField(max_length=32, blank=True)
    exchange = models.CharField(max_length=3, choices=Exchange.choices, db_index=True)
    horizon_trading_days = models.PositiveSmallIntegerField()
    window_label = models.CharField(max_length=16, help_text="e.g. 30 / 90 / 180 / 365")
    window_size = models.PositiveIntegerField()
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)

    sample_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=Status.choices, db_index=True)
    reasons = models.JSONField(default=list, blank=True)
    recommendations = models.JSONField(default=list, blank=True)

    metrics = models.JSONField(default=dict, blank=True)
    confidence_intervals = models.JSONField(default=dict, blank=True)
    drift = models.JSONField(default=dict, blank=True)
    threshold_config = models.JSONField(default=dict, blank=True)
    failure_detail = models.TextField(blank=True)

    class Meta:
        ordering = ["-run_at"]
        indexes = [
            models.Index(fields=["model_family", "exchange", "horizon_trading_days", "window_label", "-run_at"]),
        ]

    def __str__(self):
        return f"{self.model_family}[{self.exchange}] h={self.horizon_trading_days} w={self.window_label} {self.status} @ {self.run_at:%Y-%m-%d %H:%M}"


class AdminAuditAction(models.TextChoices):
    PIPELINE_TRIGGERED = "pipeline_triggered", "Pipeline triggered"
    MODEL_ACTIVATED = "model_activated", "Model activated"
    MODEL_DEACTIVATED = "model_deactivated", "Model deactivated"
    # --- Account management (role-management system) ---------------------
    ACCOUNT_CREATED = "account_created", "Account created"
    ACCOUNT_ACTIVATED = "account_activated", "Account activated"
    ACCOUNT_DEACTIVATED = "account_deactivated", "Account deactivated"
    ACCOUNT_DELETED = "account_deleted", "Account deleted"
    ROLE_PROMOTED = "role_promoted", "Role promoted"
    ROLE_DEMOTED = "role_demoted", "Role demoted"
    PASSWORD_RESET_INITIATED = "password_reset_initiated", "Password reset initiated"
    TEMP_PASSWORD_ASSIGNED = "temp_password_assigned", "Temporary password assigned"
    FIRST_LOGIN_PASSWORD_CHANGED = "first_login_password_changed", "First-login password changed"
    TELEGRAM_REPORT_FORCED = "telegram_report_forced", "Telegram ML report force-resent"


class AdminAuditLog(models.Model):
    """Who did what, staff-side, for actions with real operational
    consequence (enqueuing a fetch/retrain job that hits external
    upstreams and rewrites data, manually flipping which trained model
    version is live, or an account-management event such as creating,
    activating, or changing the role of another account). Append-only by
    convention — see AdminAuditLogAdmin, which denies delete/change
    permission entirely.

    `username_snapshot` / `target_username_snapshot` are captured at
    write time (not just the FKs) so the record stays legible even if
    either account is later deleted — both FKs use SET_NULL, not
    CASCADE, for the same reason: the audit trail must outlive the
    accounts it describes. `detail` never carries a plaintext password,
    password hash, complete reset token, session cookie, authorization
    header, or secret environment value — see market.services.audit.
    """

    user = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="admin_audit_logs"
    )
    username_snapshot = models.CharField(max_length=150, blank=True)
    target_user = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="admin_audit_logs_received"
    )
    target_username_snapshot = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=32, choices=AdminAuditAction.choices, db_index=True)
    detail = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    request_id = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        who = self.username_snapshot or "system"
        target = f" -> {self.target_username_snapshot}" if self.target_username_snapshot else ""
        return f"{who} {self.action}{target} @ {self.created_at:%Y-%m-%d %H:%M}"


class Portfolio(models.Model):
    """A named collection of BUY/SELL transactions for one user. Holdings,
    cost basis and P/L are never stored here — they're derived on demand
    from the full PortfolioTransaction history by
    market.services.portfolio, so there's nothing that can drift out of
    sync with the transaction log (see that module's docstring for why)."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="portfolios")
    name = models.CharField(max_length=64, default="Default")
    currency = models.CharField(max_length=8, default="BDT")
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("user", "name")
        ordering = ["-is_default", "name"]
        constraints = [
            # SQLite/Postgres both support partial unique indexes — enforces
            # "at most one default portfolio per user" at the DB level, not
            # just in application code (get_or_create_default_portfolio()
            # still does the friendly get-or-create dance on top of this).
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(is_default=True),
                name="unique_default_portfolio_per_user",
            ),
        ]

    def __str__(self):
        return f"{self.user.username}: {self.name}"


class TransactionType(models.TextChoices):
    BUY = "BUY", "Buy"
    SELL = "SELL", "Sell"


class PortfolioTransaction(models.Model):
    """One row per BUY or SELL fill — the complete, append-mostly ledger a
    portfolio's holdings/cost-basis/P&L are computed from. Editing or
    deleting a row is allowed (mistakes happen), but every mutation is
    re-validated against the *whole* chronological sequence for that
    (portfolio, stock) — see market.services.portfolio.validate_no_negative_holding
    — so an edit that would retroactively make a later SELL exceed the
    held quantity is rejected rather than silently producing a negative
    holding.

    Stock uses PROTECT, not CASCADE: a stock must never be deleted out
    from under a user's real financial transaction history."""

    portfolio = models.ForeignKey(Portfolio, on_delete=models.CASCADE, related_name="transactions")
    stock = models.ForeignKey(Stock, on_delete=models.PROTECT, related_name="portfolio_transactions")
    transaction_type = models.CharField(max_length=4, choices=TransactionType.choices)
    quantity = models.DecimalField(max_digits=18, decimal_places=4)
    price_per_share = models.DecimalField(max_digits=14, decimal_places=4)
    fees = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("0"))
    transaction_date = models.DateField(db_index=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-transaction_date", "-created_at"]
        indexes = [
            models.Index(fields=["portfolio", "stock"]),
            models.Index(fields=["portfolio", "transaction_date"]),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gt=0), name="portfolio_txn_quantity_positive"),
            models.CheckConstraint(condition=models.Q(price_per_share__gte=0), name="portfolio_txn_price_non_negative"),
            models.CheckConstraint(condition=models.Q(fees__gte=0), name="portfolio_txn_fees_non_negative"),
        ]

    def __str__(self):
        return f"{self.transaction_type} {self.quantity} {self.stock.trading_code} @ {self.price_per_share} ({self.transaction_date})"

    @property
    def gross_amount(self) -> Decimal:
        """Quantity x price, before fees."""
        return self.quantity * self.price_per_share
