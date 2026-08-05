import json
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse


class TickerJsonReadOnlyTests(TestCase):
    """ticker_json requires authentication (see accounts/roles.py) but must
    still serve cached/DB state only — it must never start a background
    sync thread or honor a public refresh param (previously: every
    request spawned a threading.Thread running maybe_sync(), and
    `?refresh=1` forced it)."""

    def setUp(self):
        cache.clear()
        self.url = reverse("ticker_json")
        user = User.objects.create_user(username="ticker_user", password="Correct-Horse-Battery-Staple-42")
        self.client.force_login(user)

    def test_plain_request_never_starts_a_thread(self):
        with mock.patch("threading.Thread.start") as mock_start:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        mock_start.assert_not_called()

    def test_refresh_param_is_ignored_not_honored(self):
        with mock.patch("threading.Thread.start") as mock_start:
            response = self.client.get(self.url, {"refresh": "1"})
        self.assertEqual(response.status_code, 200)
        mock_start.assert_not_called()

    def test_response_is_valid_json_from_db_only(self):
        response = self.client.get(self.url)
        payload = json.loads(response.content)
        self.assertIn("dse", payload)
        self.assertIn("cse", payload)
        self.assertIn("sync", payload)


@override_settings(TICKER_JSON_RATE_LIMIT=(2, 60))
class TickerJsonThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.url = reverse("ticker_json")
        user = User.objects.create_user(username="ticker_user2", password="Correct-Horse-Battery-Staple-42")
        self.client.force_login(user)

    def test_exceeding_rate_limit_returns_429(self):
        for _ in range(2):
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, 200)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 429)
