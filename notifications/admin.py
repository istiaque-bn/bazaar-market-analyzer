from django.contrib import admin

from notifications.models import AdminReminder, Alert

admin.site.register(Alert)


@admin.register(AdminReminder)
class AdminReminderAdmin(admin.ModelAdmin):
    list_display = ("remind_on", "action", "admin", "telegram_enabled", "email_enabled", "delivered_at")
    list_filter = ("telegram_enabled", "email_enabled", "delivered_at")
    search_fields = ("action", "admin__username")
    ordering = ("remind_on", "id")
