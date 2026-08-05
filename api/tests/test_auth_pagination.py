from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from rest_framework.authtoken.models import Token

from market.models import Exchange, Stock


class AlertListAuthenticationTests(TestCase):
    """/api/alerts/ requires authentication — anonymous requests must be
    rejected, not silently return an empty/global list."""

    def setUp(self):
        self.url = reverse("api_alerts")
        self.user = User.objects.create_user(username="alice", password="Correct-Horse-Battery-Staple-42")
        self.token = Token.objects.create(user=self.user)

    def test_anonymous_request_rejected(self):
        response = self.client.get(self.url)
        self.assertIn(response.status_code, (401, 403))

    def test_token_authenticated_request_succeeds(self):
        response = self.client.get(self.url, HTTP_AUTHORIZATION=f"Token {self.token.key}")
        self.assertEqual(response.status_code, 200)

    def test_invalid_token_rejected(self):
        response = self.client.get(self.url, HTTP_AUTHORIZATION="Token not-a-real-token")
        self.assertIn(response.status_code, (401, 403))


class LoginTokenIssuanceTests(TestCase):
    def setUp(self):
        self.url = reverse("api_login")
        self.user = User.objects.create_user(username="bob", password="Correct-Horse-Battery-Staple-42")

    def test_correct_credentials_return_token(self):
        response = self.client.post(self.url, {"username": "bob", "password": "Correct-Horse-Battery-Staple-42"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("token", response.json())

    def test_wrong_password_rejected(self):
        response = self.client.post(self.url, {"username": "bob", "password": "wrong-password"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("token", response.json())

    def test_unknown_username_rejected(self):
        response = self.client.post(self.url, {"username": "nobody-here", "password": "whatever"})
        self.assertEqual(response.status_code, 400)


class PaginationTests(TestCase):
    """DEFAULT_PAGINATION_CLASS=PageNumberPagination, PAGE_SIZE=50."""

    def setUp(self):
        user = User.objects.create_user(username="pagination_user", password="Correct-Horse-Battery-Staple-42")
        self.client.force_login(user)
        for i in range(60):
            Stock.objects.create(exchange=Exchange.DSE, trading_code=f"CODE{i:03d}", company_name=f"Co {i}")

    def test_first_page_has_50_results_and_a_next_link(self):
        response = self.client.get(reverse("api_stocks"))
        body = response.json()
        self.assertEqual(body["count"], 60)
        self.assertEqual(len(body["results"]), 50)
        self.assertIsNotNone(body["next"])
        self.assertIsNone(body["previous"])

    def test_second_page_has_remaining_10_results(self):
        first = self.client.get(reverse("api_stocks")).json()
        response = self.client.get(first["next"])
        body = response.json()
        self.assertEqual(len(body["results"]), 10)
        self.assertIsNone(body["next"])
        self.assertIsNotNone(body["previous"])


class ErrorResponseTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(username="error_resp_user", password="Correct-Horse-Battery-Staple-42")
        self.client.force_login(user)
        self.stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="REAL", company_name="Real Co")

    def test_stock_detail_404_for_unknown_code(self):
        url = reverse("api_stock_detail", args=["DSE", "NOPE"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_stock_analysis_404_for_unknown_code(self):
        url = reverse("api_stock_detail", args=["DSE", "NOPE"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_predict_price_missing_date_param_400(self):
        url = reverse("api_predict_price", args=["DSE", "REAL"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 400)
        self.assertIn("date", response.json()["error"].lower())

    def test_predict_price_malformed_date_400(self):
        url = reverse("api_predict_price", args=["DSE", "REAL"])
        response = self.client.get(url, {"date": "not-a-date"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid date", response.json()["error"])

    def test_predict_price_unknown_stock_404(self):
        url = reverse("api_predict_price", args=["DSE", "NOPE"])
        response = self.client.get(url, {"date": "2026-01-01"})
        self.assertEqual(response.status_code, 404)


class AnalysisListFilterTests(TestCase):
    def setUp(self):
        from market.models import AnalysisResult, SignalAction

        user = User.objects.create_user(username="analysis_filter_user", password="Correct-Horse-Battery-Staple-42")
        self.client.force_login(user)
        self.dse = Stock.objects.create(exchange=Exchange.DSE, trading_code="D1", company_name="D1")
        self.cse = Stock.objects.create(exchange=Exchange.CSE, trading_code="C1", company_name="C1")
        AnalysisResult.objects.create(stock=self.dse, as_of="2026-01-01", action=SignalAction.BUY, score=50)
        AnalysisResult.objects.create(stock=self.cse, as_of="2026-01-01", action=SignalAction.SELL, score=-30)

    def test_filter_by_exchange(self):
        response = self.client.get(reverse("api_analysis"), {"exchange": "CSE"})
        body = response.json()
        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(body["results"][0]["stock"]["exchange"], "CSE")

    def test_filter_by_action(self):
        response = self.client.get(reverse("api_analysis"), {"action": "sell"})
        body = response.json()
        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(body["results"][0]["action"], "SELL")
