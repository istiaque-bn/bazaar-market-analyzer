from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse


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

    def test_strong_password_creates_user_and_token(self):
        response = self.client.post(
            self.url,
            {"username": "stronguser", "password": "Correct-Horse-Battery-Staple-42", "email": "s@example.com"},
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(User.objects.filter(username="stronguser").exists())
        self.assertIn("token", response.json())
