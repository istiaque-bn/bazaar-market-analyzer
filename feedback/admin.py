from django.contrib import admin

from feedback.models import Feedback, FeedbackEvent


class FeedbackEventInline(admin.TabularInline):
    model = FeedbackEvent
    extra = 0
    readonly_fields = [f.name for f in FeedbackEvent._meta.fields]
    can_delete = False


@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):
    list_display = ("reference_number", "category", "title", "reporter_username_snapshot", "reporter_priority", "admin_priority", "status", "created_at")
    list_filter = ("category", "status", "reporter_priority", "admin_priority")
    search_fields = ("reference_number", "title", "description", "reporter_username_snapshot")
    readonly_fields = ("reference_number", "reporter_username_snapshot", "created_at", "updated_at")
    date_hierarchy = "created_at"
    inlines = [FeedbackEventInline]


@admin.register(FeedbackEvent)
class FeedbackEventAdmin(admin.ModelAdmin):
    list_display = ("feedback", "event_type", "actor_username_snapshot", "created_at")
    list_filter = ("event_type",)
    readonly_fields = [f.name for f in FeedbackEvent._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
