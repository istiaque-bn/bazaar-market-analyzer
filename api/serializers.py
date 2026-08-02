from rest_framework import serializers

from market.models import AnalysisResult, BacktestRun, PatternHit, ReliabilityAssessment, Stock, TechnicalSnapshot
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
