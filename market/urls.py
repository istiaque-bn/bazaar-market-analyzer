from django.urls import path

from market import views

urlpatterns = [
    path("", views.home, name="home"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("stocks/", views.stock_list, name="stock_list"),
    path("stocks/<str:exchange>/<str:code>/", views.stock_detail, name="stock_detail"),
    path("stocks/<str:exchange>/<str:code>/predict-price/", views.predict_price_view, name="predict_price"),
    path("stocks/<str:exchange>/<str:code>/watch/", views.toggle_watchlist, name="toggle_watchlist"),
    path("watchlist/", views.watchlist_view, name="watchlist"),
    path("backtests/", views.backtests_view, name="backtests"),
    path("alerts/", views.alerts_view, name="alerts"),
    path("pipeline/", views.run_pipeline_view, name="run_pipeline"),
    path("data-quality/", views.data_quality_view, name="data_quality"),
    path("ops/", views.ops_report_view, name="ops_report"),
    path("health/", views.health, name="health"),
    path("health/live/", views.liveness_view, name="health_live"),
    path("health/ready/", views.readiness_view, name="health_ready"),
    path("ticker.json", views.ticker_json, name="ticker_json"),
]
