from django.apps import AppConfig


class MarketConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "market"

    def ready(self):
        try:
            from market.services.autosync import start_autosync_thread
            from market.services.daily_append import start_daily_append_thread

            start_autosync_thread()
            start_daily_append_thread()
        except Exception:
            pass
