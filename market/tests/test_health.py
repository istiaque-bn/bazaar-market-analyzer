"""Phase 9 — liveness/readiness endpoints."""
from unittest import mock

from django.test import SimpleTestCase, TestCase


class LivenessViewTests(SimpleTestCase):
    # SimpleTestCase raises if a test tries to touch the database at all
    # (Django's own "Database queries ... not allowed" guard) — the
    # strongest available proof that the liveness view never does.
    def test_liveness_is_always_200_and_touches_nothing(self):
        resp = self.client.get("/health/live/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "alive"})


class ReadinessViewTests(TestCase):
    def test_ready_when_all_dependencies_ok(self):
        with mock.patch("market.services.health.check_broker", return_value=True):
            resp = self.client.get("/health/ready/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ready")
        self.assertTrue(data["checks"]["database"])
        self.assertTrue(data["checks"]["broker"])

    def test_not_ready_when_broker_down(self):
        with mock.patch("market.services.health.check_broker", return_value=False):
            resp = self.client.get("/health/ready/")
        self.assertEqual(resp.status_code, 503)
        data = resp.json()
        self.assertEqual(data["status"], "not_ready")
        self.assertFalse(data["checks"]["broker"])

    def test_not_ready_when_database_down(self):
        with mock.patch("market.services.health.check_database", return_value=False), mock.patch(
            "market.services.health.check_broker", return_value=True
        ):
            resp = self.client.get("/health/ready/")
        self.assertEqual(resp.status_code, 503)
        self.assertFalse(resp.json()["checks"]["database"])

    def test_readiness_failure_response_never_includes_exception_text(self):
        """Requirement: readiness must check dependencies without
        exposing sensitive details — a raw exception message (which
        could include a connection string/host/port) must never reach
        the HTTP response body."""

        import redis as redis_module

        with mock.patch.object(redis_module.Redis, "from_url", side_effect=ConnectionError("secret-host leaked")):
            resp = self.client.get("/health/ready/")
        self.assertEqual(resp.status_code, 503)
        body = resp.content.decode()
        self.assertNotIn("secret-host", body)
        self.assertNotIn("Traceback", body)
