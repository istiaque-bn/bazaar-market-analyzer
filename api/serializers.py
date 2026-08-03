from rest_framework import serializers

from market.models import AnalysisResult, BacktestRun, PatternHit, Portfolio, PortfolioTransaction, ReliabilityAssessment, Stock, TechnicalSnapshot
from notifications.models import Alert


class StockSerializer(serializers.ModelSerializer):
    class Meta:
        model = Stock
        fields = (
            "id",
            "exchange",
            "trading_code",
            "company_name",
            "sector",
            "group",
            "last_price",
            "last_change_pct",
            "last_volume",
            "pe_ratio",
            "eps",
        )


class AnalysisSerializer(serializers.ModelSerializer):
    stock = StockSerializer(read_only=True)
    expected_peak_price = serializers.SerializerMethodField()
    # Presentation-layer rename only — the underlying model field
    # (is_safe_buy) is unchanged to avoid a migration; "safe" is
    # guaranteed-sounding language this API must not present to consumers.
    is_experimental_candidate = serializers.BooleanField(source="is_safe_buy", read_only=True)
    signal_status = serializers.SerializerMethodField()

    class Meta:
        model = AnalysisResult
        fields = (
            "id",
            "stock",
            "as_of",
            "action",
            "score",
            "confidence",
            "risk_level",
            "is_experimental_candidate",
            "maturity_days_est",
            "peak_days_est",
            "expected_return_pct",
            "expected_peak_price",
            "probability",
            "rationale",
            "features",
            "ml_score",
            "signal_status",
        )

    def get_expected_peak_price(self, obj):
        return obj.expected_peak_price

    def get_signal_status(self, obj):
        """Model status / has-an-edge gate + plain-language context — the
        same market.services.signal_status output the web UI renders, so
        API consumers see consistent, honest language rather than a bare
        score. Callers may pass precomputed ml/close statuses via
        context (see api/views.py) to avoid recomputing them per row in
        a list response; falls back to computing fresh for a single
        object."""
        from market.services.signal_status import build_signal_status, close_learn_edge_status, ml_model_status

        ml_by_exchange = self.context.get("ml_status_by_exchange")
        close_status = self.context.get("close_status")
        if ml_by_exchange is None or close_status is None:
            ml_status = ml_model_status(obj.stock.exchange)
            close_status = close_learn_edge_status()
        else:
            ml_status = ml_by_exchange.get(obj.stock.exchange) or ml_model_status(obj.stock.exchange)
        return build_signal_status(obj.stock, obj, ml_status, close_status)


class PatternSerializer(serializers.ModelSerializer):
    class Meta:
        model = PatternHit
        fields = ("name", "direction", "strength", "description", "as_of")


class TechnicalSerializer(serializers.ModelSerializer):
    class Meta:
        model = TechnicalSnapshot
        fields = "__all__"


class BacktestSerializer(serializers.ModelSerializer):
    class Meta:
        model = BacktestRun
        fields = "__all__"


class ReliabilityAssessmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReliabilityAssessment
        fields = "__all__"


class AlertSerializer(serializers.ModelSerializer):
    class Meta:
        model = Alert
        fields = ("id", "title", "message", "channel", "is_read", "created_at")


class DecimalStringField(serializers.Field):
    """Every monetary/quantity value in the portfolio API is exact Decimal
    math (see market.services.portfolio) — rendering it as a JSON number
    risks a client's JSON parser silently round-tripping it through a
    float. A string is the one representation every client renders
    byte-for-byte as computed."""

    def to_representation(self, value):
        return str(value) if value is not None else None

    def to_internal_value(self, data):
        raise NotImplementedError("read-only computed field")


class PortfolioSerializer(serializers.ModelSerializer):
    class Meta:
        model = Portfolio
        fields = ("id", "name", "currency", "is_default", "created_at", "updated_at")
        read_only_fields = ("id", "is_default", "created_at", "updated_at")


class PortfolioTransactionSerializer(serializers.ModelSerializer):
    stock = StockSerializer(read_only=True)
    stock_id = serializers.PrimaryKeyRelatedField(queryset=Stock.objects.filter(is_active=True), source="stock", write_only=True)

    class Meta:
        model = PortfolioTransaction
        fields = (
            "id", "portfolio", "stock", "stock_id", "transaction_type", "quantity",
            "price_per_share", "fees", "transaction_date", "notes", "created_at", "updated_at",
        )
        read_only_fields = ("id", "portfolio", "created_at", "updated_at")


class HoldingSerializer(serializers.Serializer):
    """Mirrors market.services.portfolio.holding_row's dict shape exactly
    — holdings are computed, not a model, so this is a plain Serializer
    rather than a ModelSerializer."""

    exchange = serializers.CharField()
    trading_code = serializers.CharField()
    company_name = serializers.CharField(allow_blank=True)
    sector = serializers.CharField(allow_blank=True)
    quantity = DecimalStringField()
    average_price = DecimalStringField()
    purchase_cost = DecimalStringField()
    fees_in_basis = DecimalStringField()
    cost_basis = DecimalStringField()
    latest_price = DecimalStringField()
    market_value = DecimalStringField()
    unrealized_pl = DecimalStringField()
    unrealized_pl_pct = DecimalStringField()
    today_pl = DecimalStringField()
    today_pl_pct = DecimalStringField()
    realized_pl = DecimalStringField()
    allocation_pct = DecimalStringField()
    quote_status = serializers.CharField()
    quote_label = serializers.CharField()
    quote_as_of = serializers.DateTimeField(allow_null=True)
    data_warning = serializers.CharField(allow_null=True)


class AllocationSliceSerializer(serializers.Serializer):
    label = serializers.CharField()
    exchange = serializers.CharField(required=False)
    value = DecimalStringField()
    pct = DecimalStringField()


class PortfolioSummarySerializer(serializers.Serializer):
    """Mirrors market.services.portfolio.portfolio_summary's dict shape."""

    open_holdings_count = serializers.IntegerField()
    total_cost_basis = DecimalStringField()
    total_market_value = DecimalStringField()
    total_unrealized_pl = DecimalStringField()
    total_unrealized_pl_pct = DecimalStringField()
    total_realized_pl = DecimalStringField()
    today_total_pl = DecimalStringField()
    best_holding = HoldingSerializer(allow_null=True)
    worst_holding = HoldingSerializer(allow_null=True)
    holdings = HoldingSerializer(many=True)
    allocation_by_stock = AllocationSliceSerializer(many=True)
    allocation_by_exchange = AllocationSliceSerializer(many=True)
    allocation_by_sector = AllocationSliceSerializer(many=True)
    has_any_data_warning = serializers.BooleanField()
