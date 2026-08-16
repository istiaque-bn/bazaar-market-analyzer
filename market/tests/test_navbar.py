"""Navbar tests: role-aware link visibility, active-page highlighting,
CSRF-protected logout, and that every page using base.html still renders
(no broken {% url %} references introduced by the navbar redesign)."""
from django.contrib.auth.models import User
from django.test import Client, TestCase

PASSWORD = "Correct-Horse-Battery-Staple-42"


def make_user(username: str, is_staff: bool = False, is_superuser: bool = False) -> User:
    return User.objects.create_user(
        username=username, password=PASSWORD, is_staff=is_staff or is_superuser, is_superuser=is_superuser
    )


class AnonymousNavTests(TestCase):
    """Anonymous visitors can use the public landing page, not app tools."""

    def test_root_loads_public_landing_page(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Make your market research easier")

    def test_login_page_has_signup_link_when_signup_is_available(self):
        html = self.client.get("/accounts/login/").content.decode()
        self.assertIn("/accounts/signup/", html)

    def test_login_page_has_no_primary_nav(self):
        html = self.client.get("/accounts/login/").content.decode()
        self.assertNotIn('id="siteNav"', html)
        self.assertNotIn('id="marketStatus"', html)


class AuthenticatedNavTests(TestCase):
    def setUp(self):
        self.user = make_user("alice")
        self.client.login(username="alice", password=PASSWORD)

    def test_watchlist_and_portfolio_links_present(self):
        html = self.client.get("/accounts/panel/user/").content.decode()
        self.assertIn('href="/watchlist/"', html)
        self.assertIn('href="/portfolio/"', html)

    def test_account_menu_shows_username_and_profile_link(self):
        html = self.client.get("/accounts/panel/user/").content.decode()
        self.assertIn("alice", html)
        self.assertIn('href="/accounts/profile/"', html)

    def test_logout_is_post_form_with_csrf_token(self):
        html = self.client.get("/accounts/panel/user/").content.decode()
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

    def test_operations_dropdown_absent_for_regular_user(self):
        html = self.client.get("/accounts/panel/user/").content.decode()
        self.assertNotIn('id="staffMenu"', html)

    def test_accounts_link_absent_for_regular_user(self):
        html = self.client.get("/accounts/panel/user/").content.decode()
        self.assertNotIn('href="/accounts/manage/"', html)


class StaffNavTests(TestCase):
    def setUp(self):
        self.user = make_user("staff_bob", is_staff=True)
        self.client.login(username="staff_bob", password=PASSWORD)

    def test_operations_dropdown_present_without_ml_reliability_or_django_admin(self):
        html = self.client.get("/accounts/panel/staff/").content.decode()
        self.assertIn('id="staffMenu"', html)
        self.assertIn('href="/data-quality/"', html)
        self.assertIn('href="/ops/"', html)
        self.assertNotIn('href="/ml-reliability/"', html)
        self.assertNotIn('href="/admin/"', html)

    def test_users_link_present_not_accounts_wording(self):
        html = self.client.get("/accounts/panel/staff/").content.decode()
        self.assertIn('href="/accounts/manage/"', html)
        self.assertIn(">Users<", html)

    def test_portfolio_watchlist_links_absent(self):
        html = self.client.get("/accounts/panel/staff/").content.decode()
        self.assertNotIn(">Portfolio<", html)
        self.assertNotIn(">Watchlist<", html)


class AdminNavTests(TestCase):
    def setUp(self):
        self.user = make_user("admin_carol", is_superuser=True)
        self.client.login(username="admin_carol", password=PASSWORD)

    def test_admin_dropdown_present_with_all_links(self):
        html = self.client.get("/accounts/panel/admin/").content.decode()
        self.assertIn('id="staffMenu"', html)
        self.assertIn('href="/data-quality/"', html)
        self.assertIn('href="/ops/"', html)
        self.assertIn('href="/ml-reliability/"', html)
        self.assertIn('href="/tools/reminders/"', html)
        self.assertIn('href="/admin/"', html)

    def test_accounts_link_present(self):
        html = self.client.get("/accounts/panel/admin/").content.decode()
        self.assertIn('href="/accounts/manage/"', html)
        self.assertIn(">Accounts<", html)


class ActiveNavStateTests(TestCase):
    def setUp(self):
        self.user = make_user("carol_active")
        self.client.login(username="carol_active", password=PASSWORD)

    def _active_anchor(self, path, href):
        html = self.client.get(path).content.decode()
        import re

        m = re.search(r'<a href="%s"[^>]*>' % re.escape(href), html)
        self.assertIsNotNone(m, f"anchor for {href} not found on {path}")
        return m.group(0)

    def test_panel_active_only_on_own_panel(self):
        self.assertIn("active", self._active_anchor("/accounts/panel/user/", "/accounts/panel/user/"))
        self.assertNotIn("active", self._active_anchor("/stocks/", "/accounts/panel/user/"))

    def test_stocks_active_on_stock_list(self):
        self.assertIn("active", self._active_anchor("/stocks/", "/stocks/"))

    def test_stocks_not_active_on_own_panel(self):
        self.assertNotIn("active", self._active_anchor("/accounts/panel/user/", "/stocks/"))

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

    def test_tools_dropdown_not_active_on_own_panel(self):
        html = self.client.get("/accounts/panel/user/").content.decode()
        self.assertNotIn("nav-dropdown-btn active", html)

    def test_aria_current_present_on_active_link(self):
        anchor = self._active_anchor("/accounts/panel/user/", "/accounts/panel/user/")
        self.assertIn('aria-current="page"', anchor)


class PageLoadSmokeTests(TestCase):
    """Every page rendering base.html must still load — a broken {% url %}
    in the navbar would 500 every page in the project, not just the nav."""

    def test_anonymous_pages_redirect_or_load(self):
        # The public product page is available; market tools require sign-in.
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        for path in ("/dashboard/",):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/stocks/").status_code, 200)
        response = self.client.get("/accounts/login/")
        self.assertEqual(response.status_code, 200)
        response = self.client.get("/accounts/signup/")
        self.assertEqual(response.status_code, 200)

    def test_authenticated_pages_load(self):
        make_user("dave")
        self.client.login(username="dave", password=PASSWORD)
        for path in (
            "/accounts/panel/user/",
            "/dashboard/",
            "/stocks/",
            "/watchlist/",
            "/portfolio/",
            "/backtests/",
            "/alerts/",
            "/accounts/profile/",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertIn(response.status_code, (200, 302))

    def test_staff_pages_load(self):
        make_user("erin", is_staff=True)
        self.client.login(username="erin", password=PASSWORD)
        for path in ("/accounts/panel/staff/", "/data-quality/", "/ops/", "/accounts/manage/"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)

    def test_admin_pages_load(self):
        make_user("frank", is_superuser=True)
        self.client.login(username="frank", password=PASSWORD)
        for path in (
            "/accounts/panel/admin/",
            "/data-quality/",
            "/ops/",
            "/ml-reliability/",
            "/accounts/manage/",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
