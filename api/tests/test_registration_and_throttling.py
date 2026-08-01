from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.throttling import ScopedRateThrottle

from market.models import AnalysisResult, Exchange, SignalAction, Stock


class RegisterAPIPasswordValidationTests(TestCase):
    """/api/auth/register/ must reject weak passwords the same way the web
    signup form (UserCreationForm + AUTH_PASSWORD_VALIDATORS) does, instead
    of calling create_user() directly and bypassing validation."""

    def setUp(self):
        self.url = reverse("api_register")

    def test_weak_password_rejected(self):
        response = self.client.post(
            self.url, {"username": "weakpw", "password": "1", "email": "weak@example.com"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(username="weakpw").exists())

    def test_weak_password_error_is_clear_and_field_scoped(self):
        response = self.client.post(
            self.url, {"username": "weakpw2", "password": "1", "email": "weak2@example.com"}
        )
        body = response.json()
        self.assertIn("password", body)
        self.assertTrue(len(body["password"]) > 0)

    def test_strong_password_creates_user_and_token(self):
        response = self.client.post(
            self.url,
            {"username": "stronguser", "password": "Correct-Horse-Battery-Staple-42", "email": "s@example.com"},
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(User.objects.filter(username="stronguser").exists())
        self.assertIn("token", response.json())


class ApiThrottlingTests(TestCase):
    """Registration, login, and prediction endpoints must enforce explicit
    rate limits (scoped throttles), independent of the codebase's default
    anon/user rates."""

    def setUp(self):
        cache.clear()

    def test_register_endpoint_is_throttled(self):
        url = reverse("api_register")
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {"register": "1/min"}):
            r1 = self.client.post(
                url, {"username": "throttle1", "password": "Correct-Horse-Battery-Staple-42", "email": "t1@example.com"}
            )
            self.assertEqual(r1.status_code, 201)
            r2 = self.client.post(
                url, {"username": "throttle2", "password": "Correct-Horse-Battery-Staple-42", "email": "t2@example.com"}
            )
            self.assertEqual(r2.status_code, 429)

    def test_login_endpoint_is_throttled(self):
        url = reverse("api_login")
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {"login": "1/min"}):
            self.client.post(url, {"username": "nobody", "password": "wrong"})
            r2 = self.client.post(url, {"username": "nobody", "password": "wrong"})
            self.assertEqual(r2.status_code, 429)

    def test_predict_endpoint_is_throttled(self):
        url = reverse("api_predict_price", args=["DSE", "NOPE"])
        with mock.patch.dict(ScopedRateThrottle.THROTTLE_RATES, {"predict": "1/min"}):
            self.client.get(url, {"date": "2026-01-01"})
            r2 = self.client.get(url, {"date": "2026-01-01"})
            self.assertEqual(r2.status_code, 429)


class ApiResearchLanguageTests(TestCase):
    """API output must not use guaranteed-sounding "safe buy" language —
    the underlying is_safe_buy model field is kept (no migration), but the
    outward JSON key/labels must not present it as a safety guarantee."""

    def setUp(self):
        self.stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="TEST", company_name="Test Co")
        AnalysisResult.objects.create(
            stock=self.stock,
            as_of="2026-01-02",
            action=SignalAction.BUY,
            score=50,
            confidence=0.6,
            is_safe_buy=True,
        )

    def test_screener_api_has_no_safe_buys_key_and_carries_disclaimer(self):
        response = self.client.get(reverse("api_screener"))
        body = response.json()
        self.assertNotIn("safe_buys", body)
        self.assertIn("research_candidates", body)
        self.assertIn("disclaimer", body)
        self.assertTrue(len(body["research_candidates"]) >= 1)
        self.assertNotIn("is_safe_buy", body["research_candidates"][0])
        self.assertIn("is_experimental_candidate", body["research_candidates"][0])
        self.assertTrue(body["research_candidates"][0]["is_experimental_candidate"])

    def test_stock_analysis_api_carries_disclaimer(self):
        url = reverse("api_stock_detail", args=["DSE", "TEST"])
        response = self.client.get(url)
        body = response.json()
        self.assertIn("disclaimer", body)
        self.assertNotIn("is_safe_buy", body["analysis"])
        self.assertIn("is_experimental_candidate", body["analysis"])
