from django.contrib import admin

from market.models import (
    AnalysisResult,
    BacktestRun,
    CloseLearnState,
    MarketSnapshot,
    NextDayCloseForecast,
    PatternHit,
    PriceHistory,
    Stock,
    TechnicalSnapshot,
    Watchlist,
)


@admin.register(Stock)
class StockAdmin(admin.ModelAdmin):
    list_display = ("trading_code", "exchange", "company_name", "sector", "group", "last_price", "last_change_pct", "is_active")
    list_filter = ("exchange", "group", "sector", "is_active")
    search_fields = ("trading_code", "company_name")


@admin.register(PriceHistory)
class PriceHistoryAdmin(admin.ModelAdmin):
    list_display = ("stock", "date", "open", "high", "low", "close", "volume")
    list_filter = ("stock__exchange",)
    date_hierarchy = "date"


@admin.register(AnalysisResult)
class AnalysisResultAdmin(admin.ModelAdmin):
    list_display = (
        "stock",
        "as_of",
        "action",
        "score",
        "confidence",
        "is_safe_buy",
        "maturity_days_est",
        "peak_days_est",
        "risk_level",
    )
    list_filter = ("action", "is_safe_buy", "risk_level", "stock__exchange")
    search_fields = ("stock__trading_code",)


admin.site.register(TechnicalSnapshot)
admin.site.register(PatternHit)
admin.site.register(BacktestRun)
admin.site.register(MarketSnapshot)
admin.site.register(Watchlist)


@admin.register(NextDayCloseForecast)
class NextDayCloseForecastAdmin(admin.ModelAdmin):
    list_display = (
        "stock",
        "as_of",
        "target_date",
        "predicted_close",
        "actual_close",
        "pct_error",
        "confidence",
        "method",
    )
    list_filter = ("stock__exchange", "method")
    date_hierarchy = "target_date"
    search_fields = ("stock__trading_code",)


@admin.register(CloseLearnState)
class CloseLearnStateAdmin(admin.ModelAdmin):
    list_display = (
        "key",
        "return_bias",
        "mae",
        "mape",
        "direction_hit_rate",
        "settled_count",
        "updated_at",
    )
