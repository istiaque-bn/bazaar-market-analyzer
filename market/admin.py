from django.contrib import admin

from market.models import (
    AdminAuditAction,
    AdminAuditLog,
    AnalysisResult,
    BacktestRun,
    CloseLearnState,
    ImportBatch,
    MarketHoliday,
    MarketSnapshot,
    MLModelVersion,
    NextDayCloseForecast,
    PatternHit,
    PriceHistory,
    PriceHistoryRevision,
    Stock,
    TaskRun,
    TechnicalSnapshot,
    Watchlist,
)
from market.services.audit import record_admin_action


@admin.register(Stock)
class StockAdmin(admin.ModelAdmin):
    list_display = ("trading_code", "exchange", "company_name", "sector", "group", "last_price", "last_change_pct", "is_active")
    list_filter = ("exchange", "group", "sector", "is_active")
    search_fields = ("trading_code", "company_name")


@admin.register(PriceHistory)
class PriceHistoryAdmin(admin.ModelAdmin):
    list_display = ("stock", "date", "open", "high", "low", "close", "volume", "source", "is_synthetic", "quality_flags")
    list_filter = ("stock__exchange", "source", "is_synthetic", "adjustment_status")
    search_fields = ("stock__trading_code",)
    date_hierarchy = "date"


@admin.register(ImportBatch)
class ImportBatchAdmin(admin.ModelAdmin):
    list_display = ("source", "exchange", "started_at", "finished_at", "row_count", "stock_count", "error")
    list_filter = ("source", "exchange")
    date_hierarchy = "started_at"
    readonly_fields = [f.name for f in ImportBatch._meta.fields]


@admin.register(PriceHistoryRevision)
class PriceHistoryRevisionAdmin(admin.ModelAdmin):
    list_display = ("stock", "date", "previous_close", "previous_source", "new_source", "reason", "recorded_at")
    list_filter = ("reason", "new_source")
    search_fields = ("stock__trading_code",)
    date_hierarchy = "recorded_at"
    readonly_fields = [f.name for f in PriceHistoryRevision._meta.fields]


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
admin.site.register(MarketSnapshot)
admin.site.register(Watchlist)


@admin.register(MarketHoliday)
class MarketHolidayAdmin(admin.ModelAdmin):
    list_display = ("date", "name", "created_at")
    search_fields = ("name",)
    date_hierarchy = "date"
    ordering = ("-date",)


@admin.register(BacktestRun)
class BacktestRunAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "exchange",
        "engine_version",
        "start_date",
        "end_date",
        "total_trades",
        "win_rate",
        "total_return_pct",
        "sharpe_ratio",
        "max_drawdown_pct",
        "created_at",
    )
    list_filter = ("exchange", "engine_version", "strategy")
    search_fields = ("name", "strategy")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in BacktestRun._meta.fields]


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


@admin.register(TaskRun)
class TaskRunAdmin(admin.ModelAdmin):
    list_display = ("task_name", "status", "started_at", "finished_at")
    list_filter = ("task_name", "status")
    search_fields = ("task_name", "error")
    date_hierarchy = "started_at"
    readonly_fields = ("task_name", "status", "started_at", "finished_at", "detail", "error")


@admin.action(description="Deactivate selected model version(s) (audited)")
def deactivate_model_versions(modeladmin, request, queryset):
    """Phase 9: a manual kill switch for a misbehaving model — e.g. live
    skill has visibly degraded (see the Ops report page) and a staff
    member wants it out of service *now*, without waiting for the next
    scheduled retrain to (maybe) downgrade it automatically. Every
    version this actually flips gets its own audit row, not just one for
    the bulk action, so a later "who deactivated model X" question has a
    precise answer."""
    changed = 0
    for version in queryset.filter(is_active=True):
        version.is_active = False
        version.save(update_fields=["is_active"])
        record_admin_action(
            request,
            AdminAuditAction.MODEL_DEACTIVATED,
            {"model_name": version.model_name, "exchange_scope": version.exchange_scope, "version": version.version},
        )
        changed += 1
    modeladmin.message_user(request, f"Deactivated {changed} model version(s).")


@admin.action(description="Reactivate selected model version(s) (audited)")
def reactivate_model_versions(modeladmin, request, queryset):
    """Counterpart to deactivate — e.g. reverting a bad manual
    deactivation, or manually restoring a previously-good version after
    rolling back a bad retrain (see docs/RUNBOOKS.md's model rollback
    section). Does not touch other versions of the same model/scope —
    ml_model.load_model()'s own precedence logic (see that module) still
    applies if more than one ends up is_active=True."""
    changed = 0
    for version in queryset.filter(is_active=False):
        version.is_active = True
        version.save(update_fields=["is_active"])
        record_admin_action(
            request,
            AdminAuditAction.MODEL_ACTIVATED,
            {"model_name": version.model_name, "exchange_scope": version.exchange_scope, "version": version.version},
        )
        changed += 1
    modeladmin.message_user(request, f"Reactivated {changed} model version(s).")


@admin.register(MLModelVersion)
class MLModelVersionAdmin(admin.ModelAdmin):
    list_display = (
        "model_name",
        "exchange_scope",
        "version",
        "status",
        "is_active",
        "data_cutoff",
        "train_rows",
        "trained_at",
    )
    list_filter = ("model_name", "exchange_scope", "status", "is_active")
    search_fields = ("model_name", "version")
    date_hierarchy = "trained_at"
    actions = [deactivate_model_versions, reactivate_model_versions]
    readonly_fields = (
        "model_name",
        "version",
        "exchange_scope",
        "status",
        "is_active",
        "data_cutoff",
        "feature_schema",
        "train_rows",
        "fold_metadata",
        "metrics",
        "file_path",
        "backup_path",
        "notes",
        "trained_at",
    )


@admin.register(AdminAuditLog)
class AdminAuditLogAdmin(admin.ModelAdmin):
    """Read-only by design — an audit trail that could be edited or
    deleted from the same admin it's supposed to be auditing isn't
    trustworthy. Rows are only ever created by
    market.services.audit.record_admin_action()."""

    list_display = ("created_at", "username_snapshot", "action", "ip_address")
    list_filter = ("action",)
    search_fields = ("username_snapshot", "ip_address")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in AdminAuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
