from rest_framework import serializers

from market.models import AnalysisResult, BacktestRun, PatternHit, Stock, TechnicalSnapshot
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
            "is_safe_buy",
            "maturity_days_est",
            "peak_days_est",
            "expected_return_pct",
            "expected_peak_price",
            "probability",
            "rationale",
            "features",
            "ml_score",
        )

    def get_expected_peak_price(self, obj):
        return obj.expected_peak_price


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


class AlertSerializer(serializers.ModelSerializer):
    class Meta:
        model = Alert
        fields = ("id", "title", "message", "channel", "is_read", "created_at")
