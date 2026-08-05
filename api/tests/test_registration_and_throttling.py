from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.throttling import ScopedRateThrottle

from market.models import AnalysisResult, Exchange, SignalAction, Stock


class RegisterAPIDisabledTests(TestCase):
    """/api/auth/register/ is a tombstone for the old public self-registration
    endpoint (see api.views.RegisterAPI) — public registration has been
    removed project-wide. It must always refuse and never create an
    account, regardless of payload or auth state."""

    def setUp(self):
        self.url = reverse("api_register")

    def test_anonymous_post_refused_and_no_account_created(self):
        response = self.client.post(
            self.url, {"username": "hopeful", "password": "Correct-Horse-Battery-Staple-42", "email": "h@example.com"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.filter(username="hopeful").exists())

    def test_authenticated_post_also_refused(self):
        User.objects.create_user(username="already_in", password="Correct-Horse-Battery-Staple-42")
        self.client.login(username="already_in", password="Correct-Horse-Battery-Staple-42")
        response = self.client.post(
            self.url, {"username": "sneaky", "password": "Correct-Horse-Battery-Staple-42", "email": "sn@example.com"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.filter(username="sneaky").exists())

    def test_no_token_issued(self):
        response = self.client.post(
            self.url, {"username": "notoken", "password": "Correct-Horse-Battery-Staple-42", "email": "nt@example.com"}
        )
        self.assertNotIn("token", response.json())


class ApiThrottlingTests(TestCase):
    """Login and prediction endpoints must enforce explicit rate limits
    (scoped throttles), independent of the codebase's default anon/user
    rates."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="throttle_user", password="Correct-Horse-Battery-Staple-42")
        self.client.force_login(self.user)

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
        user = User.objects.create_user(username="research_lang_user", password="Correct-Horse-Battery-Staple-42")
        self.client.force_login(user)
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
