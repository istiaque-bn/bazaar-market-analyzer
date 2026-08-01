"""Phase 9 — the staff-only /ops/ report page."""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse


class OpsReportViewAccessTests(TestCase):
    def setUp(self):
        self.url = reverse("ops_report")

    def test_anonymous_is_redirected_to_login(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)

    def test_ordinary_authenticated_user_is_redirected(self):
        User.objects.create_user(username="alice", password="Correct-Horse-Battery-Staple-42")
        self.client.login(username="alice", password="Correct-Horse-Battery-Staple-42")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_staff_can_view_the_report(self):
        staff = User.objects.create_user(username="staffer", password="Correct-Horse-Battery-Staple-42")
        staff.is_staff = True
        staff.save(update_fields=["is_staff"])
        self.client.login(username="staffer", password="Correct-Horse-Battery-Staple-42")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Operational readiness", response.content)
