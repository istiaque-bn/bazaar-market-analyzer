"""Navbar tests: link visibility per auth/staff state, active-page
highlighting, CSRF-protected logout, and that every page using base.html
still renders (no broken {% url %} references introduced by the navbar
redesign)."""
from django.contrib.auth.models import User
from django.test import Client, TestCase

PASSWORD = "Correct-Horse-Battery-Staple-42"


def make_user(username: str, is_staff: bool = False) -> User:
    return User.objects.create_user(username=username, password=PASSWORD, is_staff=is_staff)


class AnonymousNavTests(TestCase):
    def test_login_and_signup_links_present(self):
        html = self.client.get("/").content.decode()
        self.assertIn('class="nav-login"', html)
        self.assertIn('href="/accounts/signup/"', html)

    def test_authenticated_only_links_absent(self):
        html = self.client.get("/").content.decode()
        self.assertNotIn("id=\"userMenu\"", html)
        self.assertNotIn(">Watchlist<", html)
        self.assertNotIn(">Profile<", html)

    def test_staff_only_admin_dropdown_absent(self):
        html = self.client.get("/").content.decode()
        self.assertNotIn('id="staffMenu"', html)

    def test_logout_form_absent_when_anonymous(self):
        html = self.client.get("/").content.decode()
        self.assertNotIn("nav-logout-form", html)


class AuthenticatedNavTests(TestCase):
    def setUp(self):
        self.user = make_user("alice")
        self.client.login(username="alice", password=PASSWORD)

    def test_watchlist_and_portfolio_links_present(self):
        html = self.client.get("/dashboard/").content.decode()
        self.assertIn('href="/watchlist/"', html)
        self.assertIn('href="/portfolio/"', html)

    def test_account_menu_shows_username_and_profile_link(self):
        html = self.client.get("/dashboard/").content.decode()
        self.assertIn("alice", html)
        self.assertIn('href="/accounts/profile/"', html)

    def test_logout_is_post_form_with_csrf_token(self):
        html = self.client.get("/dashboard/").content.decode()
        self.assertIn('<form class="nav-logout-form" method="post" action="/accounts/logout/">', html)
        self.assertIn("csrfmiddlewaretoken", html)

    def test_logout_rejects_get(self):
        response = self.client.get("/accounts/logout/")
        self.assertNotEqual(response.status_code, 200)

    def test_logout_post_without_csrf_token_is_rejected(self):
        strict_client = Client(enforce_csrf_checks=True)
        strict_client.login(username="alice", password=PASSWORD)
        response = strict_client.post("/accounts/logout/")
        self.assertEqual(response.status_code, 403)

    def test_staff_only_admin_dropdown_absent_for_regular_user(self):
        html = self.client.get("/dashboard/").content.decode()
        self.assertNotIn('id="staffMenu"', html)


class StaffNavTests(TestCase):
    def setUp(self):
        self.user = make_user("admin_bob", is_staff=True)
        self.client.login(username="admin_bob", password=PASSWORD)

    def test_admin_dropdown_present_with_all_three_links(self):
        html = self.client.get("/dashboard/").content.decode()
        self.assertIn('id="staffMenu"', html)
        self.assertIn('href="/data-quality/"', html)
        self.assertIn('href="/ops/"', html)
        self.assertIn('href="/ml-reliability/"', html)


class ActiveNavStateTests(TestCase):
    def setUp(self):
        self.user = make_user("carol")
        self.client.login(username="carol", password=PASSWORD)

    def _active_anchor(self, path, href):
        html = self.client.get(path).content.decode()
        import re

        m = re.search(r'<a href="%s"[^>]*>' % re.escape(href), html)
        self.assertIsNotNone(m, f"anchor for {href} not found on {path}")
        return m.group(0)

    def test_dashboard_active_only_on_dashboard(self):
        self.assertIn("active", self._active_anchor("/dashboard/", "/dashboard/"))
        self.assertNotIn("active", self._active_anchor("/stocks/", "/dashboard/"))

    def test_stocks_active_on_stock_list(self):
        self.assertIn("active", self._active_anchor("/stocks/", "/stocks/"))

    def test_stocks_not_active_on_dashboard(self):
        self.assertNotIn("active", self._active_anchor("/dashboard/", "/stocks/"))

    def test_watchlist_active_on_watchlist_page(self):
        self.assertIn("active", self._active_anchor("/watchlist/", "/watchlist/"))

    def test_portfolio_active_on_portfolio_pages(self):
        # portfolio_redirect (/portfolio/) 302s to the default portfolio detail;
        # the Portfolio link must be active on that redirect target too.
        detail_resp = self.client.get("/portfolio/", follow=True)
        html = detail_resp.content.decode()
        import re

        m = re.search(r'<a href="/portfolio/"[^>]*>', html)
        self.assertIsNotNone(m)
        self.assertIn("active", m.group(0))

    def test_tools_dropdown_active_on_backtests_and_alerts(self):
        html_backtests = self.client.get("/backtests/").content.decode()
        html_alerts = self.client.get("/alerts/").content.decode()
        self.assertIn("nav-dropdown-btn active", html_backtests)
        self.assertIn("nav-dropdown-btn active", html_alerts)

    def test_tools_dropdown_not_active_on_dashboard(self):
        html = self.client.get("/dashboard/").content.decode()
        self.assertNotIn("nav-dropdown-btn active", html)

    def test_aria_current_present_on_active_link(self):
        anchor = self._active_anchor("/dashboard/", "/dashboard/")
        self.assertIn('aria-current="page"', anchor)


class PageLoadSmokeTests(TestCase):
    """Every page rendering base.html must still load — a broken {% url %}
    in the navbar would 500 every page in the project, not just the nav."""

    def test_anonymous_pages_load(self):
        for path in ("/", "/stocks/", "/accounts/login/", "/accounts/signup/"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)

    def test_authenticated_pages_load(self):
        make_user("dave")
        self.client.login(username="dave", password=PASSWORD)
        for path in ("/dashboard/", "/stocks/", "/watchlist/", "/portfolio/", "/backtests/", "/alerts/", "/accounts/profile/"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertIn(response.status_code, (200, 302))

    def test_staff_pages_load(self):
        make_user("erin", is_staff=True)
        self.client.login(username="erin", password=PASSWORD)
        for path in ("/data-quality/", "/ops/", "/ml-reliability/"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
