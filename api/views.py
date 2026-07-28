from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.contrib.auth.password_validation import validate_password
from rest_framework import generics, permissions, status
from rest_framework.authtoken.models import Token
from rest_framework.authtoken.views import ObtainAuthToken
from rest_framework.response import Response
from rest_framework.views import APIView

from api.serializers import (
    AlertSerializer,
    AnalysisSerializer,
    BacktestSerializer,
    PatternSerializer,
    StockSerializer,
    TechnicalSerializer,
)
from market.models import AnalysisResult, BacktestRun, PatternHit, Stock, TechnicalSnapshot
from market.services.indicators import prices_to_df
from market.services.predictor import CONFIDENCE_SCALE, predict_price_at_date
from market.services.screener import potential_shares, safe_buys, screen_summary, sell_candidates
from notifications.models import Alert


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


class ScreenerAPI(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        return Response(
            {
                "summary": screen_summary(),
                "potential": AnalysisSerializer(potential_shares(20), many=True).data,
                "safe_buys": AnalysisSerializer(safe_buys(10), many=True).data,
                "sells": AnalysisSerializer(sell_candidates(10), many=True).data,
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
                "stock": StockSerializer(stock).data,
                "analysis": AnalysisSerializer(analysis).data if analysis else None,
                "technicals": TechnicalSerializer(tech).data if tech else None,
                "patterns": PatternSerializer(patterns, many=True).data,
            }
        )


class PredictPriceAPI(APIView):
    permission_classes = [permissions.AllowAny]

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
            return Response({"detail": exc.messages}, status=400)
        user = User.objects.create_user(username=username, password=password, email=email)
        token, _ = Token.objects.get_or_create(user=user)
        return Response({"token": token.key, "username": user.username}, status=status.HTTP_201_CREATED)


class CustomAuthToken(ObtainAuthToken):
    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        token = Token.objects.get(key=response.data["token"])
        return Response({"token": token.key, "username": token.user.username})
