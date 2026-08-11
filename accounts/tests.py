from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from accounts.roles import is_admin, is_regular_user, is_staff_member, role_name
from market.models import AdminAuditAction, AdminAuditLog

PASSWORD = "Correct-Horse-Battery-Staple-42"


def make_admin(username="admin") -> User:
    return User.objects.create_user(username=username, password=PASSWORD, is_staff=True, is_superuser=True)


def make_staff(username="staff") -> User:
    return User.objects.create_user(username=username, password=PASSWORD, is_staff=True)


def make_user(username="user") -> User:
    return User.objects.create_user(username=username, password=PASSWORD)


class ProfileMinScoreAlertTests(TestCase):
    """A non-numeric min_score_alert must show a form error, not 500."""

    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="Correct-Horse-Battery-Staple-42")
        self.client.force_login(self.user)
        self.url = reverse("profile")

    def test_non_numeric_min_score_alert_does_not_crash(self):
        response = self.client.post(self.url, {"min_score_alert": "not-a-number"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "must be a number")

    def test_valid_min_score_alert_saves(self):
        response = self.client.post(self.url, {"min_score_alert": "55"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(UserProfile.objects.get(user=self.user).min_score_alert, 55.0)

    def test_min_score_alert_above_boundary_rejected(self):
        response = self.client.post(self.url, {"min_score_alert": "150"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(UserProfile.objects.get(user=self.user).min_score_alert, 40)

    def test_min_score_alert_below_boundary_rejected(self):
        response = self.client.post(self.url, {"min_score_alert": "-5"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(UserProfile.objects.get(user=self.user).min_score_alert, 40)

    def test_unknown_exchange_rejected(self):
        response = self.client.post(self.url, {"min_score_alert": "40", "preferred_exchanges": "DSE,XYZ"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Unknown exchange")
        self.assertEqual(UserProfile.objects.get(user=self.user).preferred_exchanges, "DSE,CSE")

    def test_valid_exchange_choice_saves(self):
        response = self.client.post(self.url, {"min_score_alert": "40", "preferred_exchanges": "cse"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(UserProfile.objects.get(user=self.user).preferred_exchanges, "CSE")


class ProfileOwnershipTests(TestCase):
    """Each user's profile is auto-created and isolated — editing one
    user's settings must never touch another user's row."""

    def setUp(self):
        self.alice = User.objects.create_user(username="alice_p", password="Correct-Horse-Battery-Staple-42")
        self.bob = User.objects.create_user(username="bob_p", password="Correct-Horse-Battery-Staple-42")

    def test_signal_creates_one_profile_per_user(self):
        self.assertEqual(UserProfile.objects.filter(user=self.alice).count(), 1)
        self.assertEqual(UserProfile.objects.filter(user=self.bob).count(), 1)

    def test_editing_own_profile_does_not_touch_other_users(self):
        bob_before = UserProfile.objects.get(user=self.bob).min_score_alert
        self.client.login(username="alice_p", password="Correct-Horse-Battery-Staple-42")
        self.client.post(reverse("profile"), {"min_score_alert": "77"})
        self.assertEqual(UserProfile.objects.get(user=self.alice).min_score_alert, 77.0)
        self.assertEqual(UserProfile.objects.get(user=self.bob).min_score_alert, bob_before)

    def test_anonymous_cannot_view_profile(self):
        response = self.client.get(reverse("profile"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)


# ---------------------------------------------------------------------------
# Role model
# ---------------------------------------------------------------------------


class RoleHelperTests(TestCase):
    def test_superuser_and_staff_is_admin(self):
        u = make_admin("a1")
        self.assertTrue(is_admin(u))
        self.assertFalse(is_staff_member(u))
        self.assertFalse(is_regular_user(u))
        self.assertEqual(role_name(u), "admin")

    def test_staff_without_superuser_is_staff(self):
        u = make_staff("s1")
        self.assertFalse(is_admin(u))
        self.assertTrue(is_staff_member(u))
        self.assertFalse(is_regular_user(u))
        self.assertEqual(role_name(u), "staff")

    def test_plain_user_is_regular_user(self):
        u = make_user("u1")
        self.assertFalse(is_admin(u))
        self.assertFalse(is_staff_member(u))
        self.assertTrue(is_regular_user(u))
        self.assertEqual(role_name(u), "user")

    def test_inactive_user_has_no_role(self):
        u = make_user("u2")
        u.is_active = False
        u.save(update_fields=["is_active"])
        self.assertFalse(is_admin(u))
        self.assertFalse(is_staff_member(u))
        self.assertFalse(is_regular_user(u))
        self.assertIsNone(role_name(u))


# ---------------------------------------------------------------------------
# Anonymous / authentication
# ---------------------------------------------------------------------------


class AnonymousAccessTests(TestCase):
    def test_root_redirects_to_login(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_dashboard_stocks_portfolio_watchlist_alerts_all_redirect(self):
        for path in ("/dashboard/", "/stocks/", "/portfolio/", "/watchlist/", "/alerts/"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response.url)

    def test_api_requires_auth(self):
        response = self.client.get("/api/stocks/")
        self.assertEqual(response.status_code, 401)

    def test_old_signup_route_never_creates_an_account(self):
        before = User.objects.count()
        response = self.client.post(
            reverse("signup"), {"username": "sneak_signup", "password1": PASSWORD, "password2": PASSWORD}
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(User.objects.count(), before)
        self.assertFalse(User.objects.filter(username="sneak_signup").exists())

    def test_signup_get_also_refused(self):
        response = self.client.get(reverse("signup"))
        self.assertEqual(response.status_code, 403)


class LoginRedirectTests(TestCase):
    def test_admin_redirected_to_dashboard(self):
        make_admin("redir_admin")
        response = self.client.post(reverse("login"), {"username": "redir_admin", "password": PASSWORD})
        self.assertRedirects(response, reverse("dashboard"))

    def test_staff_redirected_to_dashboard(self):
        make_staff("redir_staff")
        response = self.client.post(reverse("login"), {"username": "redir_staff", "password": PASSWORD})
        self.assertRedirects(response, reverse("dashboard"))

    def test_user_redirected_to_dashboard(self):
        make_user("redir_user")
        response = self.client.post(reverse("login"), {"username": "redir_user", "password": PASSWORD})
        self.assertRedirects(response, reverse("dashboard"))

    def test_authenticated_visiting_login_is_redirected_to_dashboard(self):
        make_user("already_in")
        self.client.login(username="already_in", password=PASSWORD)
        response = self.client.get(reverse("login"))
        self.assertRedirects(response, reverse("dashboard"))

    def test_inactive_user_cannot_log_in(self):
        u = make_user("inactive_login")
        u.is_active = False
        u.save(update_fields=["is_active"])
        response = self.client.post(reverse("login"), {"username": "inactive_login", "password": PASSWORD})
        self.assertEqual(response.status_code, 200)  # re-renders form, no session created
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_unsafe_external_next_is_rejected(self):
        make_user("safe_next_user")
        response = self.client.post(
            reverse("login") + "?next=https://evil.example.com",
            {"username": "safe_next_user", "password": PASSWORD, "next": "https://evil.example.com"},
        )
        self.assertNotEqual(response.url, "https://evil.example.com")
        self.assertRedirects(response, reverse("dashboard"))

    def test_protocol_relative_next_is_rejected(self):
        make_user("safe_next_user2")
        response = self.client.post(
            reverse("login"),
            {"username": "safe_next_user2", "password": PASSWORD, "next": "//evil.example.com/phish"},
        )
        self.assertRedirects(response, reverse("dashboard"))

    def test_safe_internal_next_is_honored(self):
        make_user("safe_next_user3")
        response = self.client.post(
            reverse("login"), {"username": "safe_next_user3", "password": PASSWORD, "next": "/stocks/"}
        )
        self.assertRedirects(response, "/stocks/")

    def test_logout_requires_post(self):
        make_user("logout_user")
        self.client.login(username="logout_user", password=PASSWORD)
        response = self.client.get(reverse("logout"))
        self.assertEqual(response.status_code, 405)

    def test_logout_post_without_csrf_rejected(self):
        make_user("logout_user2")
        strict = Client(enforce_csrf_checks=True)
        strict.login(username="logout_user2", password=PASSWORD)
        response = strict.post(reverse("logout"))
        self.assertEqual(response.status_code, 403)


class NoStoreAfterLogoutTests(TestCase):
    """Authenticated pages must be marked uncacheable so the browser's
    back-forward cache can't replay them after logout."""

    def test_authenticated_page_is_marked_no_store(self):
        make_user("nostore_user")
        self.client.login(username="nostore_user", password=PASSWORD)
        response = self.client.get(reverse("dashboard"))
        self.assertIn("no-store", response["Cache-Control"])

    def test_page_is_no_store_again_after_logout(self):
        make_user("nostore_user2")
        self.client.login(username="nostore_user2", password=PASSWORD)
        self.client.post(reverse("logout"))
        response = self.client.get(reverse("dashboard"))
        self.assertNotIn("no-store", response.get("Cache-Control", ""))
        self.assertRedirects(response, f"{reverse('login')}?next={reverse('dashboard')}")


class SessionSecurityTests(TestCase):
    def setUp(self):
        self.user = make_user("idle_session_user")
        self.client.login(username="idle_session_user", password=PASSWORD)

    def test_browser_session_cookie_has_no_persistent_expiry(self):
        response = self.client.get(reverse("dashboard"))
        cookie = response.cookies["sessionid"]
        self.assertEqual(cookie.get("max-age", ""), "")
        self.assertEqual(cookie.get("expires", ""), "")

    def test_user_is_logged_out_after_30_minutes_without_activity(self):
        from accounts.middleware import _IDLE_TIMEOUT_SECONDS

        self.client.get(reverse("dashboard"))
        session = self.client.session
        session["bazaar_last_activity_at"] = timezone.now().timestamp() - _IDLE_TIMEOUT_SECONDS
        session.save()

        response = self.client.get(reverse("stock_list"), follow=True)
        self.assertEqual(response.request["PATH_INFO"], reverse("login"))
        self.assertFalse(response.wsgi_request.user.is_authenticated)
        self.assertContains(response, "30 minutes of inactivity")

    def test_activity_renews_the_idle_window(self):
        self.client.get(reverse("dashboard"))
        session = self.client.session
        session["bazaar_last_activity_at"] = timezone.now().timestamp() - (29 * 60)
        session.save()

        response = self.client.get(reverse("stock_list"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.wsgi_request.user.is_authenticated)


class DeactivatedAccountSessionTests(TestCase):
    """A deactivated account must lose access on its very next request,
    even mid-session (see accounts.middleware.AccountStateMiddleware)."""

    def test_deactivated_mid_session_is_bounced_to_login(self):
        u = make_user("live_session")
        self.client.login(username="live_session", password=PASSWORD)
        ok = self.client.get(reverse("user_panel"))
        self.assertEqual(ok.status_code, 200)
        u.is_active = False
        u.save(update_fields=["is_active"])
        bounced = self.client.get(reverse("user_panel"), follow=True)
        self.assertEqual(bounced.status_code, 200)
        self.assertEqual(bounced.request["PATH_INFO"], reverse("login"))


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


class AdminAccountManagementTests(TestCase):
    def setUp(self):
        self.admin = make_admin("mgmt_admin")
        self.client.login(username="mgmt_admin", password=PASSWORD)

    def test_admin_can_create_user(self):
        response = self.client.post(
            reverse("account_create_user"),
            {
                "username": "new_plain_user",
                "first_name": "N",
                "last_name": "U",
                "email": "npu@example.com",
                "telegram_chat_id": "100200301",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 200)
        u = User.objects.get(username="new_plain_user")
        self.assertFalse(u.is_staff)
        self.assertFalse(u.is_superuser)
        self.assertTrue(u.profile.must_change_password)
        self.assertEqual(u.profile.created_by, self.admin)

    def test_admin_can_create_staff(self):
        response = self.client.post(
            reverse("account_create_staff"),
            {
                "username": "new_staff",
                "first_name": "N",
                "last_name": "S",
                "email": "ns@example.com",
                "telegram_chat_id": "100200302",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 200)
        u = User.objects.get(username="new_staff")
        self.assertTrue(u.is_staff)
        self.assertFalse(u.is_superuser)

    def test_admin_can_deactivate_and_reactivate_user(self):
        target = make_user("toggle_target")
        self.client.post(reverse("account_deactivate", args=[target.id]), {"confirm_username": "toggle_target"})
        target.refresh_from_db()
        self.assertFalse(target.is_active)
        self.client.post(reverse("account_activate", args=[target.id]))
        target.refresh_from_db()
        self.assertTrue(target.is_active)

    def test_admin_can_promote_user_and_demote_staff(self):
        target = make_user("promote_target")
        self.client.post(reverse("account_promote", args=[target.id]), {"confirm_username": "promote_target"})
        target.refresh_from_db()
        self.assertTrue(target.is_staff)
        self.assertFalse(target.is_superuser)

        self.client.post(reverse("account_demote", args=[target.id]), {"confirm_username": "promote_target"})
        target.refresh_from_db()
        self.assertFalse(target.is_staff)

    def test_final_active_admin_cannot_be_deactivated(self):
        response = self.client.post(
            reverse("account_deactivate", args=[self.admin.id]), {"confirm_username": "mgmt_admin"}, follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_non_final_admin_can_be_deactivated(self):
        second_admin = make_admin("second_admin")
        response = self.client.post(
            reverse("account_deactivate", args=[second_admin.id]), {"confirm_username": "second_admin"}, follow=True
        )
        self.assertEqual(response.status_code, 200)
        second_admin.refresh_from_db()
        self.assertFalse(second_admin.is_active)

    def test_admin_tools_reachable(self):
        for name in ("admin_panel", "account_list", "data_quality", "ops_report", "ml_reliability"):
            with self.subTest(name=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)

    def test_sensitive_actions_generate_audit_events(self):
        target = make_user("audit_target")
        self.client.post(reverse("account_deactivate", args=[target.id]), {"confirm_username": "audit_target"})
        self.assertTrue(
            AdminAuditLog.objects.filter(
                action=AdminAuditAction.ACCOUNT_DEACTIVATED, target_user=target, user=self.admin
            ).exists()
        )

    def test_account_creation_generates_audit_event(self):
        self.client.post(
            reverse("account_create_user"),
            {
                "username": "audited_create",
                "first_name": "",
                "last_name": "",
                "email": "",
                "telegram_chat_id": "100200303",
                "is_active": "on",
            },
        )
        u = User.objects.get(username="audited_create")
        self.assertTrue(
            AdminAuditLog.objects.filter(action=AdminAuditAction.ACCOUNT_CREATED, target_user=u).exists()
        )


class TempPasswordTelegramTests(TestCase):
    """Username/temp-password delivery via Telegram, and the 15-minute
    expiry on that temp password as a login credential."""

    def setUp(self):
        self.admin = make_admin("tg_admin")
        self.client.login(username="tg_admin", password=PASSWORD)

    def test_telegram_chat_id_is_required_to_create_an_account(self):
        response = self.client.post(
            reverse("account_create_user"),
            {"username": "no_chat_id_user", "first_name": "", "last_name": "", "email": "", "is_active": "on"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="no_chat_id_user").exists())
        self.assertContains(response, "Telegram chat ID")

    def test_non_numeric_chat_id_is_rejected(self):
        response = self.client.post(
            reverse("account_create_user"),
            {
                "username": "bad_chat_id_user",
                "first_name": "",
                "last_name": "",
                "email": "",
                "telegram_chat_id": "not-a-number",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="bad_chat_id_user").exists())

    @override_settings(TELEGRAM_BOT_TOKEN="main-bot-token", TELEGRAM_SECURITY_BOT_TOKEN="security-bot-token")
    @mock.patch("accounts.services.send_telegram_message", return_value=True)
    def test_creation_sends_username_and_temp_password_via_telegram(self, mock_send):
        self.client.post(
            reverse("account_create_user"),
            {
                "username": "tg_new_user",
                "first_name": "",
                "last_name": "",
                "email": "",
                "telegram_chat_id": "555000111",
                "is_active": "on",
            },
        )
        u = User.objects.get(username="tg_new_user")
        self.assertEqual(u.profile.telegram_chat_id, "555000111")
        self.assertIsNotNone(u.profile.temp_password_expires_at)
        self.assertEqual(mock_send.call_count, 2)
        chat_id, text = mock_send.call_args_list[0][0]
        self.assertEqual(chat_id, "555000111")
        self.assertIn("tg_new_user", text)
        self.assertIn("15 minutes", text)
        # Delivered through the dedicated security bot, not the main one.
        self.assertEqual(mock_send.call_args_list[0].kwargs["token"], "security-bot-token")

    @mock.patch("accounts.services.send_telegram_message", return_value=False)
    def test_creation_still_shows_password_on_screen_if_telegram_delivery_fails(self, mock_send):
        response = self.client.post(
            reverse("account_create_user"),
            {
                "username": "tg_fail_user",
                "first_name": "",
                "last_name": "",
                "email": "",
                "telegram_chat_id": "555000112",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(User.objects.filter(username="tg_fail_user").exists())
        self.assertContains(response, "temp-password-value")

    @override_settings(EMAIL_HOST="smtp.example.test", DEFAULT_FROM_EMAIL="noreply@example.test")
    @mock.patch("accounts.services.send_mail", return_value=1)
    @mock.patch("accounts.services.send_telegram_message", return_value=True)
    def test_creation_sends_temp_password_by_email_when_smtp_is_configured(self, _mock_telegram, mock_mail):
        response = self.client.post(
            reverse("account_create_user"),
            {
                "username": "email_new_user",
                "first_name": "",
                "last_name": "",
                "email": "new.user@example.test",
                "telegram_chat_id": "555000114",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "also sent by Telegram and email")
        mock_mail.assert_called_once()
        self.assertEqual(mock_mail.call_args.kwargs["recipient_list"], ["new.user@example.test"])
        self.assertEqual(mock_mail.call_args.kwargs["from_email"], "noreply@example.test")
        self.assertIn("email_new_user", mock_mail.call_args.kwargs["message"])

    @mock.patch("accounts.services.send_telegram_message", return_value=True)
    def test_reset_password_sends_new_temp_password_via_telegram(self, mock_send):
        target = make_user("tg_reset_target")
        target.profile.telegram_chat_id = "555000113"
        target.profile.save(update_fields=["telegram_chat_id"])
        self.client.post(reverse("account_reset_password", args=[target.id]))
        mock_send.assert_called_once()
        chat_id, text = mock_send.call_args[0]
        self.assertEqual(chat_id, "555000113")
        self.assertIn("tg_reset_target", text)
        target.profile.refresh_from_db()
        self.assertIsNotNone(target.profile.temp_password_expires_at)

    def test_expired_temp_password_cannot_be_used_to_log_in(self):
        target = make_user("tg_expired_target")
        target.set_password("Temp-Pass-1234!")
        target.save(update_fields=["password"])
        target.profile.must_change_password = True
        target.profile.temp_password_expires_at = timezone.now() - timedelta(minutes=1)
        target.profile.save(update_fields=["must_change_password", "temp_password_expires_at"])

        client = Client()
        response = client.post(reverse("login"), {"username": "tg_expired_target", "password": "Temp-Pass-1234!"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "expired")
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_unexpired_temp_password_can_still_log_in(self):
        target = make_user("tg_fresh_target")
        target.set_password("Temp-Pass-5678!")
        target.save(update_fields=["password"])
        target.profile.must_change_password = True
        target.profile.temp_password_expires_at = timezone.now() + timedelta(minutes=10)
        target.profile.save(update_fields=["must_change_password", "temp_password_expires_at"])

        client = Client()
        response = client.post(
            reverse("login"), {"username": "tg_fresh_target", "password": "Temp-Pass-5678!"}, follow=True
        )
        self.assertEqual(response.request["PATH_INFO"], reverse("force_password_change"))

    def test_password_change_clears_the_expiry(self):
        target = make_user("tg_clear_target")
        target.set_password("Temp-Pass-9999!")
        target.save(update_fields=["password"])
        target.profile.must_change_password = True
        target.profile.temp_password_expires_at = timezone.now() + timedelta(minutes=10)
        target.profile.save(update_fields=["must_change_password", "temp_password_expires_at"])

        client = Client()
        client.login(username="tg_clear_target", password="Temp-Pass-9999!")
        client.post(
            reverse("force_password_change"),
            {"new_password1": "Brand-New-Pass-42!", "new_password2": "Brand-New-Pass-42!"},
        )
        target.profile.refresh_from_db()
        self.assertFalse(target.profile.must_change_password)
        self.assertIsNone(target.profile.temp_password_expires_at)


# ---------------------------------------------------------------------------
# Staff
# ---------------------------------------------------------------------------


class StaffAccountManagementTests(TestCase):
    def setUp(self):
        self.staff = make_staff("mgmt_staff")
        self.client.login(username="mgmt_staff", password=PASSWORD)

    def test_staff_can_create_user(self):
        response = self.client.post(
            reverse("account_create_user"),
            {
                "username": "staff_created_user",
                "first_name": "",
                "last_name": "",
                "email": "",
                "telegram_chat_id": "100200304",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 200)
        u = User.objects.get(username="staff_created_user")
        self.assertFalse(u.is_staff)

    def test_staff_cannot_create_staff(self):
        response = self.client.get(reverse("account_create_staff"))
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            reverse("account_create_staff"),
            {
                "username": "staff_tries_staff",
                "first_name": "",
                "last_name": "",
                "email": "",
                "telegram_chat_id": "100200305",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.filter(username="staff_tries_staff").exists())

    def test_manipulated_privilege_fields_are_ignored(self):
        response = self.client.post(
            reverse("account_create_user"),
            {
                "username": "staff_manip",
                "first_name": "",
                "last_name": "",
                "email": "",
                "telegram_chat_id": "100200306",
                "is_active": "on",
                "is_staff": "on",
                "is_superuser": "on",
                "role": "admin",
                "groups": "1",
            },
        )
        self.assertEqual(response.status_code, 200)
        u = User.objects.get(username="staff_manip")
        self.assertFalse(u.is_staff)
        self.assertFalse(u.is_superuser)

    def test_staff_cannot_modify_admin_account(self):
        admin = make_admin("target_admin")
        response = self.client.get(reverse("account_detail", args=[admin.id]))
        self.assertEqual(response.status_code, 404)
        response = self.client.post(reverse("account_deactivate", args=[admin.id]), {"confirm_username": "target_admin"})
        self.assertEqual(response.status_code, 404)
        admin.refresh_from_db()
        self.assertTrue(admin.is_active)

    def test_staff_cannot_modify_another_staff_account(self):
        other_staff = make_staff("other_staff")
        response = self.client.get(reverse("account_detail", args=[other_staff.id]))
        self.assertEqual(response.status_code, 404)

    def test_staff_cannot_promote_or_demote_anyone(self):
        target = make_user("staff_promote_target")
        response = self.client.post(
            reverse("account_promote", args=[target.id]), {"confirm_username": "staff_promote_target"}
        )
        self.assertEqual(response.status_code, 403)
        target.refresh_from_db()
        self.assertFalse(target.is_staff)

    def test_staff_cannot_promote_self(self):
        response = self.client.post(
            reverse("account_promote", args=[self.staff.id]), {"confirm_username": "mgmt_staff"}
        )
        self.assertEqual(response.status_code, 403)
        self.staff.refresh_from_db()
        self.assertFalse(self.staff.is_superuser)

    def test_staff_cannot_access_admin_only_tools(self):
        for name in ("ml_reliability", "run_pipeline", "admin_panel"):
            with self.subTest(name=name):
                response = self.client.get(reverse(name)) if name != "run_pipeline" else self.client.post(
                    reverse(name), {"mode": "analyze"}
                )
                self.assertEqual(response.status_code, 403)

    def test_staff_sees_staff_panel_and_nav(self):
        response = self.client.get(reverse("staff_panel"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Staff Panel")


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------


class RegularUserAccessTests(TestCase):
    def setUp(self):
        self.alice = make_user("ru_alice")
        self.bob = make_user("ru_bob")
        self.client.login(username="ru_alice", password=PASSWORD)

    def test_user_sees_user_panel(self):
        response = self.client.get(reverse("user_panel"))
        self.assertEqual(response.status_code, 200)

    def test_user_cannot_access_account_management(self):
        response = self.client.get(reverse("account_list"))
        self.assertEqual(response.status_code, 403)
        response = self.client.get(reverse("account_create_user"))
        self.assertEqual(response.status_code, 403)

    def test_user_cannot_access_staff_or_admin_tools(self):
        for name in ("staff_panel", "admin_panel", "data_quality", "ops_report", "ml_reliability"):
            with self.subTest(name=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 403)

    def test_user_cannot_trigger_pipeline(self):
        response = self.client.post(reverse("run_pipeline"), {"mode": "analyze"})
        self.assertEqual(response.status_code, 403)

    def test_cross_user_profile_isolated(self):
        from accounts.models import UserProfile

        UserProfile.objects.filter(user=self.bob).update(min_score_alert=99)
        self.client.post(reverse("profile"), {"min_score_alert": "12"})
        self.assertEqual(UserProfile.objects.get(user=self.bob).min_score_alert, 99)
        self.assertEqual(UserProfile.objects.get(user=self.alice).min_score_alert, 12.0)

    def test_cross_user_portfolio_access_denied(self):
        from market.services.portfolio import get_or_create_default_portfolio

        bob_portfolio = get_or_create_default_portfolio(self.bob)
        response = self.client.get(reverse("portfolio_detail", args=[bob_portfolio.id]))
        self.assertEqual(response.status_code, 404)

    def test_cross_user_watchlist_isolated(self):
        from market.models import Exchange, Stock, Watchlist

        stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="ISOX", company_name="Isolation Co")
        bob_wl, _ = Watchlist.objects.get_or_create(user=self.bob, name="Default")
        bob_wl.stocks.add(stock)
        alice_wl, _ = Watchlist.objects.get_or_create(user=self.alice, name="Default")
        self.assertFalse(alice_wl.stocks.filter(id=stock.id).exists())
        response = self.client.get(reverse("watchlist"))
        self.assertNotContains(response, "ISOX")

    def test_cross_user_alert_isolated(self):
        from notifications.models import Alert, AlertChannel

        Alert.objects.create(user=self.bob, channel=AlertChannel.IN_APP, title="Bob only", message="private")
        response = self.client.get(reverse("alerts"))
        titles = {a.title for a in response.context["alerts"]}
        self.assertNotIn("Bob only", titles)


class ApiPrivilegeEscalationTests(TestCase):
    """API mustn't let a payload bypass web-form-level role restrictions,
    and portfolio ownership must hold via the API the same as the web."""

    def test_regular_user_cannot_create_account_via_api(self):
        """There is no account-management API surface at all (see
        api/urls.py) — confirms it stays that way rather than a stray
        endpoint reappearing and bypassing the web view's role checks."""
        make_user("api_esc_user")
        client = Client()
        client.login(username="api_esc_user", password=PASSWORD)
        response = client.post("/api/accounts/", {"username": "hacker", "is_staff": True})
        self.assertEqual(response.status_code, 404)

    def test_portfolio_api_rejects_foreign_user_field(self):
        from market.services.portfolio import get_or_create_default_portfolio

        alice = make_user("api_esc_alice")
        bob = make_user("api_esc_bob")
        get_or_create_default_portfolio(alice)
        client = Client()
        client.login(username="api_esc_bob", password=PASSWORD)
        response = client.post("/api/portfolios/", {"name": "Bob Portfolio", "user": alice.id})
        self.assertEqual(response.status_code, 201)
        from market.models import Portfolio

        created = Portfolio.objects.get(name="Bob Portfolio")
        self.assertEqual(created.user_id, bob.id)  # server-assigned, payload's user ignored
