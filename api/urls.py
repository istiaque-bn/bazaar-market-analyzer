from django.urls import path

from api import views

urlpatterns = [
    path("auth/register/", views.RegisterAPI.as_view(), name="api_register"),
    path("auth/login/", views.CustomAuthToken.as_view(), name="api_login"),
    path("stocks/", views.StockListAPI.as_view(), name="api_stocks"),
    path("stocks/<str:exchange>/<str:code>/", views.StockAnalysisAPI.as_view(), name="api_stock_detail"),
    path("stocks/<str:exchange>/<str:code>/predict-price/", views.PredictPriceAPI.as_view(), name="api_predict_price"),
    path("analysis/", views.AnalysisListAPI.as_view(), name="api_analysis"),
    path("screener/", views.ScreenerAPI.as_view(), name="api_screener"),
    path("backtests/", views.BacktestListAPI.as_view(), name="api_backtests"),
    path("alerts/", views.AlertListAPI.as_view(), name="api_alerts"),
    path("ml-reliability/", views.MLReliabilityAPI.as_view(), name="api_ml_reliability"),
    path("portfolios/", views.PortfolioListAPI.as_view(), name="api_portfolio_list"),
    path("portfolios/<int:pk>/", views.PortfolioDetailAPI.as_view(), name="api_portfolio_detail"),
    path("portfolios/<int:portfolio_id>/summary/", views.PortfolioSummaryAPI.as_view(), name="api_portfolio_summary"),
    path("portfolios/<int:portfolio_id>/holdings/", views.PortfolioHoldingsAPI.as_view(), name="api_portfolio_holdings"),
    path("portfolios/<int:portfolio_id>/quote-snapshot/", views.PortfolioQuoteSnapshotAPI.as_view(), name="api_portfolio_quote_snapshot"),
    path(
        "portfolios/<int:portfolio_id>/transactions/",
        views.PortfolioTransactionListAPI.as_view(),
        name="api_portfolio_transactions",
    ),
    path(
        "portfolios/<int:portfolio_id>/transactions/<int:txn_id>/",
        views.PortfolioTransactionDetailAPI.as_view(),
        name="api_portfolio_transaction_detail",
    ),
]
