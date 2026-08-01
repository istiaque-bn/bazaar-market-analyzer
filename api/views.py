from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.contrib.auth.password_validation import validate_password
from rest_framework import generics, permissions, status
from rest_framework.authtoken.models import Token
from rest_framework.authtoken.views import ObtainAuthToken
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from api.serializers import (
    AlertSerializer,
    AnalysisSerializer,
    BacktestSerializer,
    PatternSerializer,
    StockSerializer,
    TechnicalSerializer,
)
from market.models import AnalysisResult, BacktestRun, Exchange, PatternHit, Stock, TechnicalSnapshot
from market.services.indicators import prices_to_df
from market.services.predictor import CONFIDENCE_SCALE, RESEARCH_DISCLAIMER, predict_price_at_date
from market.services.screener import potential_shares, safe_buys, screen_summary, sell_candidates
from market.services.signal_status import close_learn_edge_status, ml_model_status
from notifications.models import Alert


def _signal_status_context() -> dict:
    """Precompute ml/close model status once per request — shared via
    serializer context so a list of N stocks doesn't run N ML-status
    lookups + N next-close skill scans (see AnalysisSerializer.get_signal_status)."""
    return {
        "ml_status_by_exchange": {
            Exchange.DSE: ml_model_status(Exchange.DSE),
            Exchange.CSE: ml_model_status(Exchange.CSE),
        },
        "close_status": close_learn_edge_status(),
    }


class StockListAPI(generics.ListAPIView):
    serializer_class = StockSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        qs = Stock.objects.filter(is_active=True)
        exchange = self.request.query_params.get("exchange")
        if exchange:
            qs = qs.filter(exchange=exchange.upper())
        q = self.request.query_params.get("q")
        if q:
            qs = qs.filter(trading_code__icontains=q)
        return qs


class StockDetailAPI(generics.RetrieveAPIView):
    serializer_class = StockSerializer
    permission_classes = [permissions.AllowAny]
    lookup_field = "trading_code"

    def get_queryset(self):
        exchange = self.kwargs.get("exchange", "DSE").upper()
        return Stock.objects.filter(exchange=exchange)


class AnalysisListAPI(generics.ListAPIView):
    serializer_class = AnalysisSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        qs = AnalysisResult.objects.select_related("stock").order_by("-as_of", "-score")
        exchange = self.request.query_params.get("exchange")
        action = self.request.query_params.get("action")
        if exchange:
            qs = qs.filter(stock__exchange=exchange.upper())
        if action:
            qs = qs.filter(action=action.upper())
        return qs[:100]

    def get_serializer_context(self):
        return {**super().get_serializer_context(), **_signal_status_context()}


class ScreenerAPI(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        ctx = _signal_status_context()
        return Response(
            {
                "disclaimer": RESEARCH_DISCLAIMER,
                "summary": screen_summary(),
                "potential": AnalysisSerializer(potential_shares(20), many=True, context=ctx).data,
                "research_candidates": AnalysisSerializer(safe_buys(10), many=True, context=ctx).data,
                "sells": AnalysisSerializer(sell_candidates(10), many=True, context=ctx).data,
            }
        )


class StockAnalysisAPI(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request, exchange, code):
        stock = Stock.objects.filter(exchange=exchange.upper(), trading_code=code.upper()).first()
        if not stock:
            return Response({"detail": "Not found"}, status=404)
        analysis = AnalysisResult.objects.filter(stock=stock).order_by("-as_of").first()
        tech = TechnicalSnapshot.objects.filter(stock=stock).order_by("-as_of").first()
        patterns = PatternHit.objects.filter(stock=stock).order_by("-as_of")[:10]
        return Response(
            {
                "disclaimer": RESEARCH_DISCLAIMER,
                "stock": StockSerializer(stock).data,
                "analysis": AnalysisSerializer(analysis).data if analysis else None,
                "technicals": TechnicalSerializer(tech).data if tech else None,
                "patterns": PatternSerializer(patterns, many=True).data,
            }
        )


class PredictPriceAPI(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "predict"

    def get(self, request, exchange, code):
        stock = Stock.objects.filter(exchange=exchange.upper(), trading_code=code.upper()).first()
        if not stock:
            return Response({"ok": False, "error": "Not found", "confidence_scale": CONFIDENCE_SCALE}, status=404)
        date_str = (request.query_params.get("date") or "").strip()
        if not date_str:
            return Response({"ok": False, "error": "Pass ?date=YYYY-MM-DD", "confidence_scale": CONFIDENCE_SCALE}, status=400)
        try:
            from datetime import datetime

            target = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return Response({"ok": False, "error": "Invalid date. Use YYYY-MM-DD.", "confidence_scale": CONFIDENCE_SCALE}, status=400)
        result = predict_price_at_date(prices_to_df(stock.prices.all()), target)
        result.setdefault("disclaimer", RESEARCH_DISCLAIMER)
        return Response(result, status=200 if result.get("ok") else 400)


class BacktestListAPI(generics.ListAPIView):
    queryset = BacktestRun.objects.all()[:20]
    serializer_class = BacktestSerializer
    permission_classes = [permissions.AllowAny]


class AlertListAPI(generics.ListAPIView):
    serializer_class = AlertSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Alert.objects.filter(user=self.request.user)[:50]


class RegisterAPI(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "register"

    def post(self, request):
        username = request.data.get("username")
        password = request.data.get("password")
        email = request.data.get("email", "")
        if not username or not password:
            return Response({"detail": "username and password required"}, status=400)
        if User.objects.filter(username=username).exists():
            return Response({"detail": "username taken"}, status=400)
        try:
            validate_password(password)
        except ValidationError as exc:
            return Response({"password": exc.messages}, status=400)
        user = User.objects.create_user(username=username, password=password, email=email)
        token, _ = Token.objects.get_or_create(user=user)
        return Response({"token": token.key, "username": user.username}, status=status.HTTP_201_CREATED)


class CustomAuthToken(ObtainAuthToken):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        token = Token.objects.get(key=response.data["token"])
        return Response({"token": token.key, "username": token.user.username})
