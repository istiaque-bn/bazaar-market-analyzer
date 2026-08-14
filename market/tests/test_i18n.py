from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse


class BanglaInterfaceTests(TestCase):
    def setUp(self):
        User.objects.create_user("bangla_user", password="Correct-Horse-Battery-Staple-42")
        self.client.login(username="bangla_user", password="Correct-Horse-Battery-Staple-42")

    def test_language_selector_sets_bangla_cookie_and_localizes_navigation(self):
        response = self.client.post(reverse("set_language"), {"language": "bn", "next": reverse("dashboard")})

        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
        self.assertIn("django_language", response.cookies)
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn('lang="bn"', html)
        self.assertIn("শেয়ারসমূহ", html)
        self.assertIn("মার্কেট ড্যাশবোর্ড", html)
        self.assertNotIn("bangla-ui.js", html)
        self.assertIn("সব এক্সচেঞ্জ", self.client.get(reverse("stock_list")).content.decode())

    def test_english_remains_the_default_language(self):
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn('lang="en"', html)
        self.assertIn("Market dashboard", html)
        self.assertNotIn("bangla-ui.js", html)
