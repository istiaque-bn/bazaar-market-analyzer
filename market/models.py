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


class PriceHistory(models.Model):
    stock = models.ForeignKey(Stock, on_delete=models.CASCADE, related_name="prices")
    date = models.DateField(db_index=True)
    open = models.FloatField()
    high = models.FloatField()
    low = models.FloatField()
    close = models.FloatField()
    volume = models.BigIntegerField(default=0)
    value = models.FloatField(default=0)

    class Meta:
        unique_together = ("stock", "date")
        ordering = ["-date"]
        verbose_name_plural = "price histories"
        indexes = [
            models.Index(fields=["stock", "-date"]),
        ]

    def __str__(self):
        return f"{self.stock.trading_code} {self.date} {self.close}"


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


class BacktestRun(models.Model):
    name = models.CharField(max_length=128)
    strategy = models.CharField(max_length=64)
    exchange = models.CharField(max_length=3, choices=Exchange.choices, blank=True)
    start_date = models.DateField()
    end_date = models.DateField()
    total_trades = models.IntegerField(default=0)
    win_rate = models.FloatField(default=0)
    avg_return_pct = models.FloatField(default=0)
    avg_days_to_peak = models.FloatField(null=True, blank=True)
    avg_days_to_target = models.FloatField(null=True, blank=True)
    max_drawdown_pct = models.FloatField(null=True, blank=True)
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
