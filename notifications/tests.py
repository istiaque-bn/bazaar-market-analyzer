from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.contrib.auth.models import User
from django.db.models import Q
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token

from accounts.models import UserProfile
from market.models import AdminAuditAction, AdminAuditLog, MLModelVersion, TaskAlertState, TaskHealth, TaskHealthStatus
from notifications.models import AdminReminder, Alert, AlertChannel, MlDailyReportDelivery, MlDailyReportStatus
from notifications.services import TelegramPermanentError, TelegramTransientError, send_telegram_message, send_telegram_message_tracked
from notifications.tasks import (
    _compare_with_previous,
    _digest_text,
    _idempotency_key,
    send_daily_digest,
    deliver_admin_reminders,
    send_market_close_notification,
    send_market_open_notification,
    send_ml_daily_report,
    send_ops_alerts_to_admin,
)

PASSWORD = "Correct-Horse-Battery-Staple-42"
FIXED_NOW_BEFORE = datetime(2026, 8, 5, 4, 0, tzinfo=dt_timezone.utc)  # 10:00 Asia/Dhaka
FIXED_NOW_AFTER = datetime(2026, 8, 5, 12, 0, tzinfo=dt_timezone.utc)  # 18:00 Asia/Dhaka


@override_settings(TELEGRAM_ADMIN_CHAT_ID="999888777")
class AdminReminderDeliveryTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username="reminder_delivery_admin", password=PASSWORD)

    @mock.patch("notifications.tasks.send_telegram_message", return_value=True)
    def test_due_reminder_creates_in_app_alert_and_telegram_once(self, mock_telegram):
        reminder = AdminReminder.objects.create(
            admin=self.admin,
            remind_on=timezone.localdate(),
            action="Check the market data pipeline.",
            telegram_enabled=True,
        )
        first = deliver_admin_reminders()
        second = deliver_admin_reminders()

        reminder.refresh_from_db()
        self.assertEqual(first["delivered"], 1)
        self.assertEqual(second["delivered"], 0)
        self.assertIsNotNone(reminder.delivered_at)
        self.assertEqual(Alert.objects.filter(user=self.admin, title="Admin reminder").count(), 1)
        mock_telegram.assert_called_once()

    @mock.patch("notifications.tasks.send_telegram_message")
    def test_reminder_can_be_in_app_only(self, mock_telegram):
        AdminReminder.objects.create(
            admin=self.admin,
            remind_on=timezone.localdate(),
            action="Read the reliability report.",
            telegram_enabled=False,
        )
        deliver_admin_reminders()
        self.assertEqual(Alert.objects.filter(user=self.admin, title="Admin reminder").count(), 1)
        mock_telegram.assert_not_called()

    @mock.patch("notifications.tasks.send_mail")
    def test_email_choice_is_saved_but_not_sent_until_email_delivery_is_enabled(self, mock_email):
        AdminReminder.objects.create(
            admin=self.admin,
            remind_on=timezone.localdate(),
            action="Email delivery will be enabled later.",
            telegram_enabled=False,
            email_enabled=True,
        )
        deliver_admin_reminders()
        mock_email.assert_not_called()


class DigestModelStatusTests(TestCase):
    """Phase 7: the digest must state plainly whether anything currently
    demonstrates a predictive edge, using the same signal_status logic
    as the web UI and API — not silently list "research candidates" as
    if they were backed by a validated model."""

    def test_digest_states_no_edge_when_nothing_deployed(self):
        text = _digest_text()
        self.assertIn("Model status: NO demonstrated predictive edge", text)

    def test_digest_states_demonstrated_edge_when_ml_active(self):
        MLModelVersion.objects.create(
            model_name="forward_return_rf",
            version="v1",
            exchange_scope="combined",
            status="active",
            is_active=True,
            data_cutoff=timezone.localdate(),
            train_rows=300,
            metrics={"skill_vs_baseline": {"majority_class": 0.08}, "model": {"direction_hit_rate": 0.58}},
        )
        text = _digest_text()
        self.assertIn("Model status: demonstrated edge", text)


@override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="")
class DailyDigestDedupTests(TestCase):
    """send_daily_digest used to create both a global Alert(user=None) and an
    identical per-user Alert(user=user); views filter on
    Q(user=request.user) | Q(user__isnull=True), so every active user saw
    the digest twice. Only the global row should exist after the task runs."""

    def setUp(self):
        self.user = User.objects.create_user(username="bob", password="Correct-Horse-Battery-Staple-42")
        UserProfile.objects.filter(user=self.user).update(email_alerts=False, telegram_alerts=False)

    @mock.patch("notifications.tasks.send_telegram_message")
    def test_active_user_sees_digest_exactly_once(self, mock_telegram):
        send_daily_digest()
        visible = Alert.objects.filter(Q(user=self.user) | Q(user__isnull=True))
        self.assertEqual(visible.count(), 1)
        self.assertIsNone(visible.first().user)
        mock_telegram.assert_not_called()

    @mock.patch("notifications.tasks.send_telegram_message")
    def test_only_one_global_alert_created(self, mock_telegram):
        send_daily_digest()
        self.assertEqual(Alert.objects.count(), 1)

    @mock.patch("notifications.tasks.send_telegram_message")
    def test_duplicate_trigger_is_idempotent_not_resent(self, mock_telegram):
        """A retry or a duplicate beat/worker fire for the same day must
        not re-send or duplicate the digest — proves task idempotency."""
        first = send_daily_digest()
        second = send_daily_digest()
        self.assertEqual(Alert.objects.count(), 1)
        self.assertIn("skipped", second)
        self.assertNotIn("skipped", first)


@override_settings(TELEGRAM_BOT_TOKEN="tok-abc", TELEGRAM_ADMIN_CHAT_ID="999888777")
class MarketSessionNotificationTests(TestCase):
    """Same idempotency shape as send_daily_digest (a per-day, user=None
    Alert row doubles as the audit record and the duplicate-send guard),
    plus the trading-day gate these two tasks add on top."""

    TRADING_DAY = date(2026, 8, 9)  # Sunday — a real DSE trading day, not a seeded holiday
    NON_TRADING_WEEKDAY = date(2026, 8, 7)  # Friday — outside TRADING_WEEKDAYS

    @mock.patch("notifications.tasks.send_telegram_message", return_value=True)
    def test_skips_on_non_trading_weekday(self, mock_send):
        with mock.patch("notifications.tasks.timezone.localdate", return_value=self.NON_TRADING_WEEKDAY):
            result = send_market_open_notification()
        self.assertEqual(result.get("skipped"), "non_trading_day")
        mock_send.assert_not_called()

    @mock.patch("market.services.trading_calendar.closure_reason", return_value="Eid holiday")
    @mock.patch("notifications.tasks.send_telegram_message", return_value=True)
    def test_skips_on_holiday(self, mock_send, mock_closure):
        with mock.patch("notifications.tasks.timezone.localdate", return_value=self.TRADING_DAY):
            result = send_market_open_notification()
        self.assertEqual(result.get("skipped"), "non_trading_day")
        mock_send.assert_not_called()

    @override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_ADMIN_CHAT_ID="")
    @mock.patch("notifications.tasks.send_telegram_message", return_value=True)
    def test_skips_when_not_configured(self, mock_send):
        with mock.patch("notifications.tasks.timezone.localdate", return_value=self.TRADING_DAY):
            result = send_market_open_notification()
        self.assertEqual(result.get("skipped"), "not_configured")
        mock_send.assert_not_called()

    @mock.patch("notifications.tasks.send_telegram_message", return_value=True)
    def test_sends_and_records_alert_on_trading_day(self, mock_send):
        with mock.patch("notifications.tasks.timezone.localdate", return_value=self.TRADING_DAY):
            result = send_market_open_notification()
        self.assertTrue(result.get("sent"))
        mock_send.assert_called_once()
        self.assertEqual(Alert.objects.filter(user__isnull=True).count(), 1)

    @mock.patch("notifications.tasks.send_telegram_message", return_value=True)
    def test_duplicate_trigger_same_day_is_idempotent(self, mock_send):
        with mock.patch("notifications.tasks.timezone.localdate", return_value=self.TRADING_DAY):
            first = send_market_open_notification()
            second = send_market_open_notification()
        self.assertNotIn("skipped", first)
        self.assertEqual(second.get("skipped"), "already_sent")
        mock_send.assert_called_once()

    @mock.patch("notifications.tasks.send_telegram_message", return_value=True)
    def test_open_and_close_are_independent_alerts(self, mock_send):
        with mock.patch("notifications.tasks.timezone.localdate", return_value=self.TRADING_DAY):
            send_market_open_notification()
            send_market_close_notification()
        self.assertEqual(Alert.objects.filter(user__isnull=True).count(), 2)
        self.assertEqual(mock_send.call_count, 2)


# ---------------------------------------------------------------------------
# Telegram operational alerts — delivery, cooldown, health-state transitions
# ---------------------------------------------------------------------------


@override_settings(TELEGRAM_BOT_TOKEN="tok-abc", TELEGRAM_ADMIN_CHAT_ID="999888777", TELEGRAM_OPS_ALERT_COOLDOWN_MINUTES=60)
class OpsAlertDeliveryTests(TestCase):
    """send_ops_alerts_to_admin used to reference an undefined
    `cooldown_start` (fixed in ffe9a39) — the bug never surfaced in tests
    because this task had no coverage at all, so a real Telegram alert
    would silently NameError every time evaluate_alerts() returned
    anything. These tests exercise the actual code path so a regression
    here fails the suite instead of failing silently in production."""

    @staticmethod
    def _one_alert(key="database_unreachable", severity="critical"):
        return [{"key": key, "severity": severity, "message": f"{key} is firing.", "detail": {}}]

    @mock.patch("notifications.tasks.send_telegram_message_tracked", return_value={"message_id": 1})
    @mock.patch("market.services.ops_alerts.evaluate_alerts")
    def test_new_alert_is_sent_without_crashing(self, mock_evaluate, mock_send):
        """The exact regression: a first-time-firing alert must reach
        Telegram and record an Alert row, not raise NameError."""
        mock_evaluate.return_value = self._one_alert()
        result = send_ops_alerts_to_admin()
        self.assertEqual(result.get("sent"), 1)
        mock_send.assert_called_once()
        self.assertTrue(
            Alert.objects.filter(
                user__isnull=True, channel=AlertChannel.TELEGRAM, title="Ops alert: database_unreachable"
            ).exists()
        )

    @mock.patch("notifications.tasks.send_telegram_message_tracked", return_value={"message_id": 1})
    @mock.patch("market.services.ops_alerts.evaluate_alerts")
    def test_repeat_alert_within_cooldown_is_suppressed(self, mock_evaluate, mock_send):
        mock_evaluate.return_value = self._one_alert()
        first = send_ops_alerts_to_admin()
        second = send_ops_alerts_to_admin()
        self.assertEqual(first.get("sent"), 1)
        self.assertEqual(second.get("skipped"), "no_new_alerts")
        mock_send.assert_called_once()
        self.assertEqual(Alert.objects.filter(channel=AlertChannel.TELEGRAM).count(), 1)

    @mock.patch("notifications.tasks.send_telegram_message_tracked", return_value={"message_id": 1})
    @mock.patch("market.services.ops_alerts.evaluate_alerts")
    def test_alert_refires_once_cooldown_window_has_passed(self, mock_evaluate, mock_send):
        mock_evaluate.return_value = self._one_alert()
        stale = Alert.objects.create(
            user=None,
            channel=AlertChannel.TELEGRAM,
            title="Ops alert: database_unreachable",
            message="stale",
            is_sent=True,
        )
        Alert.objects.filter(pk=stale.pk).update(created_at=timezone.now() - timedelta(minutes=61))
        result = send_ops_alerts_to_admin()
        self.assertEqual(result.get("sent"), 1)
        mock_send.assert_called_once()

    @mock.patch("notifications.tasks.send_telegram_message_tracked", return_value={"message_id": 1})
    @mock.patch("market.services.ops_alerts.evaluate_alerts")
    def test_repeated_failure_alerts_never_reach_telegram(self, mock_evaluate, mock_send):
        """Covered instead by the TaskHealth failure/recovery state
        machine below — sending both would double-page for one outage."""
        mock_evaluate.return_value = self._one_alert(key="repeated_failure_market.tasks.fetch_all_market_data")
        result = send_ops_alerts_to_admin()
        self.assertEqual(result.get("skipped"), "no_new_alerts")
        mock_send.assert_not_called()

    @mock.patch("notifications.tasks.send_telegram_message_tracked", return_value={"message_id": 1})
    @mock.patch("market.services.ops_alerts.evaluate_alerts", return_value=[])
    def test_failure_pending_task_health_notifies_and_becomes_failure_sent(self, mock_evaluate, mock_send):
        health = TaskHealth.objects.create(
            task_name="market.tasks.fetch_all_market_data",
            current_health_status=TaskHealthStatus.UNHEALTHY,
            consecutive_failures=3,
            last_error="boom",
            alert_state=TaskAlertState.FAILURE_PENDING,
        )
        result = send_ops_alerts_to_admin()
        self.assertEqual(result.get("sent"), 1)
        mock_send.assert_called_once()
        health.refresh_from_db()
        self.assertEqual(health.alert_state, TaskAlertState.FAILURE_SENT)
        self.assertIsNotNone(health.last_alert)

    @mock.patch("notifications.tasks.send_telegram_message_tracked", return_value={"message_id": 1})
    @mock.patch("market.services.ops_alerts.evaluate_alerts", return_value=[])
    def test_recovery_pending_task_health_notifies_and_clears_to_none(self, mock_evaluate, mock_send):
        health = TaskHealth.objects.create(
            task_name="market.tasks.fetch_all_market_data",
            current_health_status=TaskHealthStatus.HEALTHY,
            alert_state=TaskAlertState.RECOVERY_PENDING,
        )
        result = send_ops_alerts_to_admin()
        self.assertEqual(result.get("sent"), 1)
        mock_send.assert_called_once()
        health.refresh_from_db()
        self.assertEqual(health.alert_state, TaskAlertState.NONE)

    @mock.patch("notifications.tasks.send_telegram_message_tracked")
    @mock.patch("market.services.ops_alerts.evaluate_alerts", return_value=[])
    def test_nothing_firing_sends_no_telegram_message(self, mock_evaluate, mock_send):
        result = send_ops_alerts_to_admin()
        self.assertEqual(result.get("skipped"), "no_new_alerts")
        mock_send.assert_not_called()

    @override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_ADMIN_CHAT_ID="")
    @mock.patch("notifications.tasks.send_telegram_message_tracked")
    @mock.patch("market.services.ops_alerts.evaluate_alerts")
    def test_skips_when_not_configured(self, mock_evaluate, mock_send):
        mock_evaluate.return_value = self._one_alert()
        result = send_ops_alerts_to_admin()
        self.assertEqual(result.get("skipped"), "not_configured")
        mock_send.assert_not_called()

    @override_settings(TELEGRAM_OPS_ALERTS=False)
    @mock.patch("notifications.tasks.send_telegram_message_tracked")
    @mock.patch("market.services.ops_alerts.evaluate_alerts")
    def test_disabled_flag_skips_entirely(self, mock_evaluate, mock_send):
        mock_evaluate.return_value = self._one_alert()
        result = send_ops_alerts_to_admin()
        self.assertEqual(result.get("skipped"), "disabled")
        mock_send.assert_not_called()


class AlertPrivacyTests(TestCase):
    """A user's personal Alert must never be visible to another user, on
    either the web view or the API — only their own + global (user=None)
    alerts."""

    def setUp(self):
        self.alice = User.objects.create_user(username="alice_alert", password="Correct-Horse-Battery-Staple-42")
        self.bob = User.objects.create_user(username="bob_alert", password="Correct-Horse-Battery-Staple-42")
        self.alice_alert = Alert.objects.create(
            user=self.alice, channel=AlertChannel.IN_APP, title="Alice-only", message="private to alice"
        )
        self.global_alert = Alert.objects.create(
            user=None, channel=AlertChannel.IN_APP, title="Global", message="everyone sees this"
        )

    def test_web_view_hides_other_users_personal_alert(self):
        self.client.login(username="bob_alert", password="Correct-Horse-Battery-Staple-42")
        response = self.client.get(reverse("alerts"))
        titles = {a.title for a in response.context["alerts"]}
        self.assertIn("Global", titles)
        self.assertNotIn("Alice-only", titles)

    def test_web_view_shows_own_personal_alert(self):
        self.client.login(username="alice_alert", password="Correct-Horse-Battery-Staple-42")
        response = self.client.get(reverse("alerts"))
        titles = {a.title for a in response.context["alerts"]}
        self.assertIn("Alice-only", titles)
        self.assertIn("Global", titles)

    def test_anonymous_is_redirected_to_login(self):
        """/alerts/ requires authentication project-wide now (see
        accounts/roles.py) — there is no more anonymous global-only view."""
        response = self.client.get(reverse("alerts"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_api_hides_other_users_personal_alert(self):
        token = Token.objects.create(user=self.bob)
        response = self.client.get(reverse("api_alerts"), HTTP_AUTHORIZATION=f"Token {token.key}")
        titles = {row["title"] for row in response.json()["results"]} if "results" in response.json() else {
            row["title"] for row in response.json()
        }
        self.assertNotIn("Alice-only", titles)

    def test_api_shows_own_personal_alert(self):
        token = Token.objects.create(user=self.alice)
        response = self.client.get(reverse("api_alerts"), HTTP_AUTHORIZATION=f"Token {token.key}")
        body = response.json()
        titles = {row["title"] for row in (body["results"] if "results" in body else body)}
        self.assertIn("Alice-only", titles)


# ---------------------------------------------------------------------------
# Telegram sender — security (redaction, timeouts, typed errors)
# ---------------------------------------------------------------------------


@override_settings(TELEGRAM_BOT_TOKEN="super-secret-token-123", TELEGRAM_CHAT_ID="")
class TelegramSenderSecurityTests(TestCase):
    def test_transient_network_error_is_redacted(self):
        import requests

        with mock.patch("requests.post", side_effect=requests.exceptions.ConnectionError("connect to https://api.telegram.org/botsuper-secret-token-123/sendMessage failed")):
            with self.assertRaises(TelegramTransientError) as ctx:
                send_telegram_message_tracked("12345", "hello")
        self.assertNotIn("super-secret-token-123", str(ctx.exception))
        self.assertIn("[REDACTED]", str(ctx.exception))

    def test_rate_limit_429_is_transient(self):
        resp = mock.Mock(status_code=429, text="Too Many Requests")
        with mock.patch("requests.post", return_value=resp):
            with self.assertRaises(TelegramTransientError):
                send_telegram_message_tracked("12345", "hello")

    def test_server_error_5xx_is_transient(self):
        resp = mock.Mock(status_code=502, text="Bad Gateway")
        with mock.patch("requests.post", return_value=resp):
            with self.assertRaises(TelegramTransientError):
                send_telegram_message_tracked("12345", "hello")

    def test_unauthorized_401_is_permanent(self):
        resp = mock.Mock(status_code=401, text="super-secret-token-123 is invalid")
        with mock.patch("requests.post", return_value=resp):
            with self.assertRaises(TelegramPermanentError) as ctx:
                send_telegram_message_tracked("12345", "hello")
        self.assertNotIn("super-secret-token-123", str(ctx.exception))

    def test_success_returns_message_id(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"ok": True, "result": {"message_id": 42}}
        with mock.patch("requests.post", return_value=resp):
            result = send_telegram_message_tracked("12345", "hello")
        self.assertEqual(result["message_id"], 42)

    @override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="")
    def test_not_configured_raises_permanent_without_network_call(self):
        with mock.patch("requests.post") as mock_post:
            with self.assertRaises(TelegramPermanentError):
                send_telegram_message_tracked("12345", "hello")
        mock_post.assert_not_called()

    def test_best_effort_sender_never_raises_and_logs_redacted(self):
        import requests

        with mock.patch("requests.post", side_effect=requests.exceptions.Timeout("timed out calling https://api.telegram.org/botsuper-secret-token-123/sendMessage")):
            with self.assertLogs("notifications.services", level="WARNING") as log_ctx:
                ok = send_telegram_message("12345", "hello")
        self.assertFalse(ok)
        self.assertNotIn("super-secret-token-123", "\n".join(log_ctx.output))

    def test_timeouts_are_set_on_the_request(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"ok": True, "result": {}}
        with mock.patch("requests.post", return_value=resp) as mock_post:
            send_telegram_message_tracked("12345", "hello")
        _args, kwargs = mock_post.call_args
        self.assertIn("timeout", kwargs)
        self.assertEqual(len(kwargs["timeout"]), 2)


# ---------------------------------------------------------------------------
# Telegram ML daily report — scheduling
# ---------------------------------------------------------------------------


class MlDailyReportSchedulingTests(TestCase):
    @override_settings(TELEGRAM_ML_DAILY_REPORT=True, TELEGRAM_ML_REPORT_TIME="17:00", TELEGRAM_ML_REPORT_TIMEZONE="Asia/Dhaka")
    def test_not_yet_time_is_skipped_automatically(self):
        with mock.patch("notifications.tasks.timezone.now", return_value=FIXED_NOW_BEFORE):
            result = send_ml_daily_report()
        self.assertEqual(result.get("skipped"), "not_yet_time")
        self.assertEqual(MlDailyReportDelivery.objects.count(), 0)

    @override_settings(TELEGRAM_ML_DAILY_REPORT=True, TELEGRAM_ML_REPORT_TIME="17:00")
    def test_manual_trigger_bypasses_not_yet_time_gate(self):
        with mock.patch("notifications.tasks.timezone.now", return_value=FIXED_NOW_BEFORE):
            result = send_ml_daily_report(manual=True)
        self.assertNotEqual(result.get("skipped"), "not_yet_time")

    @override_settings(TELEGRAM_ML_DAILY_REPORT=False)
    def test_disabled_flag_skips_entirely(self):
        result = send_ml_daily_report()
        self.assertEqual(result.get("skipped"), "disabled")
        self.assertEqual(MlDailyReportDelivery.objects.count(), 0)

    def test_non_trading_day_still_generates_a_short_report_not_an_error(self):
        with mock.patch("notifications.tasks.timezone.now", return_value=FIXED_NOW_AFTER), \
                mock.patch("market.services.ml_daily_report.closure_reason", return_value="Friday"):
            result = send_ml_daily_report(manual=True)
        self.assertTrue(result.get("ok"))
        delivery = MlDailyReportDelivery.objects.get()
        self.assertIn("non-trading day", delivery.report_text)


# ---------------------------------------------------------------------------
# Telegram ML daily report — idempotency / delivery record
# ---------------------------------------------------------------------------


class MlDailyReportIdempotencyKeyTests(TestCase):
    def test_deterministic_per_recipient_and_date(self):
        d = timezone.localdate()
        self.assertEqual(_idempotency_key(d, "12345"), _idempotency_key(d, "12345"))

    def test_different_recipients_get_different_keys(self):
        d = timezone.localdate()
        self.assertNotEqual(_idempotency_key(d, "12345"), _idempotency_key(d, "67890"))

    def test_recipient_is_not_stored_raw_in_the_key(self):
        key = _idempotency_key(timezone.localdate(), "12345678")
        self.assertNotIn("12345678", key)
        self.assertTrue(key.startswith("telegram_ml_daily_report:"))


@override_settings(TELEGRAM_BOT_TOKEN="tok-abc", TELEGRAM_ADMIN_CHAT_ID="999888777")
class MlDailyReportDeliveryTests(TestCase):
    def test_successful_send_records_sent_delivery(self):
        with mock.patch("notifications.tasks.timezone.now", return_value=FIXED_NOW_AFTER), \
                mock.patch("notifications.tasks.send_telegram_message_tracked", return_value={"message_id": 42}):
            result = send_ml_daily_report(manual=True)
        self.assertTrue(result.get("sent"))
        delivery = MlDailyReportDelivery.objects.get()
        self.assertEqual(delivery.status, MlDailyReportStatus.SENT)
        self.assertEqual(delivery.telegram_message_id, "42")
        self.assertNotIn("999888777", delivery.recipient_masked)

    def test_duplicate_trigger_same_day_does_not_resend(self):
        with mock.patch("notifications.tasks.timezone.now", return_value=FIXED_NOW_AFTER), \
                mock.patch("notifications.tasks.send_telegram_message_tracked", return_value={"message_id": 1}) as mock_send:
            send_ml_daily_report(manual=True)
            second = send_ml_daily_report(manual=True)
        self.assertEqual(second.get("skipped"), "already_sent")
        mock_send.assert_called_once()
        self.assertEqual(MlDailyReportDelivery.objects.count(), 1)

    def test_force_resend_sends_again_without_creating_a_second_row(self):
        with mock.patch("notifications.tasks.timezone.now", return_value=FIXED_NOW_AFTER), \
                mock.patch("notifications.tasks.send_telegram_message_tracked", return_value={"message_id": 1}) as mock_send:
            send_ml_daily_report(manual=True)
            result = send_ml_daily_report(manual=True, force=True)
        self.assertTrue(result.get("sent"))
        self.assertEqual(mock_send.call_count, 2)
        self.assertEqual(MlDailyReportDelivery.objects.count(), 1)

    def test_permanent_failure_marks_failed_and_does_not_raise(self):
        with mock.patch("notifications.tasks.timezone.now", return_value=FIXED_NOW_AFTER), \
                mock.patch("notifications.tasks.send_telegram_message_tracked", side_effect=TelegramPermanentError("chat not found")):
            result = send_ml_daily_report(manual=True)
        self.assertFalse(result.get("ok"))
        delivery = MlDailyReportDelivery.objects.get()
        self.assertEqual(delivery.status, MlDailyReportStatus.FAILED)

    def test_transient_failure_marks_retrying_and_reraises_for_autoretry(self):
        with mock.patch("notifications.tasks.timezone.now", return_value=FIXED_NOW_AFTER), \
                mock.patch("notifications.tasks.send_telegram_message_tracked", side_effect=TelegramTransientError("timeout")):
            with self.assertRaises(TelegramTransientError):
                send_ml_daily_report(manual=True)
        delivery = MlDailyReportDelivery.objects.get()
        self.assertEqual(delivery.status, MlDailyReportStatus.RETRYING)

    def test_confirmed_delivery_survives_a_later_retry_call_without_duplicating(self):
        """A retried task invocation after confirmed delivery must not
        resend — same guarantee as test_duplicate_trigger_same_day, from
        the "retry after success" angle specifically."""
        with mock.patch("notifications.tasks.timezone.now", return_value=FIXED_NOW_AFTER), \
                mock.patch("notifications.tasks.send_telegram_message_tracked", return_value={"message_id": 7}) as mock_send:
            send_ml_daily_report(manual=True)
            retried = send_ml_daily_report()  # simulates Celery re-invoking after the fact
        self.assertEqual(retried.get("skipped"), "already_sent")
        mock_send.assert_called_once()


@override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_ADMIN_CHAT_ID="")
class MlDailyReportNotConfiguredTests(TestCase):
    def test_generates_and_stores_report_without_sending(self):
        with mock.patch("notifications.tasks.timezone.now", return_value=FIXED_NOW_AFTER):
            result = send_ml_daily_report(manual=True)
        self.assertEqual(result.get("skipped"), "not_configured")
        delivery = MlDailyReportDelivery.objects.get()
        self.assertEqual(delivery.status, MlDailyReportStatus.SKIPPED)
        self.assertTrue(delivery.report_text)


class MlDailyReportSecretHandlingTests(TestCase):
    @override_settings(TELEGRAM_BOT_TOKEN="super-secret-token-xyz", TELEGRAM_ADMIN_CHAT_ID="12345678")
    def test_delivery_record_never_contains_the_bot_token(self):
        with mock.patch("notifications.tasks.timezone.now", return_value=FIXED_NOW_AFTER), \
                mock.patch("notifications.tasks.send_telegram_message_tracked", return_value={"message_id": 1}):
            send_ml_daily_report(manual=True)
        delivery = MlDailyReportDelivery.objects.get()
        haystack = f"{delivery.report_text}|{delivery.last_error}|{delivery.detail}|{delivery.recipient_masked}|{delivery.model_version_summary}"
        self.assertNotIn("super-secret-token-xyz", haystack)

    @override_settings(TELEGRAM_BOT_TOKEN="super-secret-token-xyz", TELEGRAM_ADMIN_CHAT_ID="12345678")
    def test_delivery_record_never_contains_the_raw_chat_id(self):
        with mock.patch("notifications.tasks.timezone.now", return_value=FIXED_NOW_AFTER), \
                mock.patch("notifications.tasks.send_telegram_message_tracked", return_value={"message_id": 1}):
            send_ml_daily_report(manual=True)
        delivery = MlDailyReportDelivery.objects.get()
        self.assertNotIn("12345678", delivery.recipient_masked)
        self.assertNotIn("12345678", delivery.idempotency_key)


# ---------------------------------------------------------------------------
# Telegram ML daily report — "what changed" comparison
# ---------------------------------------------------------------------------


def _ctx(*, model_version_tag="v1", window_label="365", live_n, live_precision, trained_today=False):
    return {
        "model_version_tag": model_version_tag,
        "window_label": window_label,
        "trained_today": trained_today,
        "live": {"n": live_n, "precision": live_precision},
    }


class MlDailyReportComparisonTests(TestCase):
    def _store_previous(self, report_date, context):
        MlDailyReportDelivery.objects.create(
            report_date=report_date,
            recipient_masked="12***78",
            idempotency_key=f"telegram_ml_daily_report:test:{report_date.isoformat()}",
            status=MlDailyReportStatus.SENT,
            detail={"snapshot": {
                "model_version_tag": context["model_version_tag"],
                "window_label": context["window_label"],
                "live_n": context["live"]["n"],
                "live_precision": context["live"]["precision"],
                "status_label": "Stable",
            }},
        )

    def test_no_previous_report_returns_none(self):
        today = timezone.localdate()
        result = _compare_with_previous(today, _ctx(live_n=150, live_precision=0.6))
        self.assertIsNone(result)

    def test_no_new_evidence_when_nothing_changed(self):
        yesterday = timezone.localdate() - __import__("datetime").timedelta(days=1)
        today = timezone.localdate()
        self._store_previous(yesterday, _ctx(live_n=150, live_precision=0.6))
        result = _compare_with_previous(today, _ctx(live_n=150, live_precision=0.6, trained_today=False))
        self.assertEqual(result["message"], "No new evidence since the previous report.")

    def test_tiny_change_described_as_stable(self):
        yesterday = timezone.localdate() - __import__("datetime").timedelta(days=1)
        today = timezone.localdate()
        self._store_previous(yesterday, _ctx(live_n=150, live_precision=0.60))
        # +1 new settled prediction and a 1-point precision wobble — well
        # under COMPARISON_TOLERANCE_PCT.
        result = _compare_with_previous(today, _ctx(live_n=151, live_precision=0.61))
        self.assertEqual(result["message"], "Performance is stable compared with the previous report.")

    def test_material_improvement_reported(self):
        yesterday = timezone.localdate() - __import__("datetime").timedelta(days=1)
        today = timezone.localdate()
        self._store_previous(yesterday, _ctx(live_n=150, live_precision=0.55))
        result = _compare_with_previous(today, _ctx(live_n=200, live_precision=0.75))
        self.assertEqual(result["message"], "Performance improved slightly compared with the previous report.")

    def test_material_decline_reported(self):
        yesterday = timezone.localdate() - __import__("datetime").timedelta(days=1)
        today = timezone.localdate()
        self._store_previous(yesterday, _ctx(live_n=150, live_precision=0.75))
        result = _compare_with_previous(today, _ctx(live_n=200, live_precision=0.40))
        self.assertEqual(result["message"], "Performance declined compared with the previous report.")

    def test_model_version_change_disclosed_not_compared_numerically(self):
        yesterday = timezone.localdate() - __import__("datetime").timedelta(days=1)
        today = timezone.localdate()
        self._store_previous(yesterday, _ctx(model_version_tag="v1", live_n=150, live_precision=0.30))
        result = _compare_with_previous(today, _ctx(model_version_tag="v2", live_n=10, live_precision=0.90))
        self.assertIn("new model version", result["message"])
        self.assertIn("not directly comparable", result["message"])

    def test_evaluation_window_change_disclosed(self):
        yesterday = timezone.localdate() - __import__("datetime").timedelta(days=1)
        today = timezone.localdate()
        self._store_previous(yesterday, _ctx(window_label="365", live_n=150, live_precision=0.30))
        result = _compare_with_previous(today, _ctx(window_label="90", live_n=90, live_precision=0.90))
        self.assertIn("not directly comparable", result["message"])


# ---------------------------------------------------------------------------
# Telegram ML daily report — Admin views (authorization)
# ---------------------------------------------------------------------------


class TelegramReportAuthorizationTests(TestCase):
    def setUp(self):
        self.preview_url = reverse("telegram_report_preview")
        self.send_url = reverse("telegram_report_send")

    def test_anonymous_redirected_from_preview(self):
        response = self.client.get(self.preview_url)
        self.assertEqual(response.status_code, 302)

    def test_anonymous_redirected_from_send(self):
        response = self.client.post(self.send_url)
        self.assertEqual(response.status_code, 302)

    def test_regular_user_forbidden_from_preview(self):
        User.objects.create_user(username="tg_u1", password=PASSWORD)
        self.client.login(username="tg_u1", password=PASSWORD)
        response = self.client.get(self.preview_url)
        self.assertEqual(response.status_code, 403)

    def test_staff_forbidden_from_preview_and_send(self):
        User.objects.create_user(username="tg_s1", password=PASSWORD, is_staff=True)
        self.client.login(username="tg_s1", password=PASSWORD)
        self.assertEqual(self.client.get(self.preview_url).status_code, 403)
        self.assertEqual(self.client.post(self.send_url).status_code, 403)

    def test_admin_can_preview(self):
        User.objects.create_user(username="tg_a1", password=PASSWORD, is_staff=True, is_superuser=True)
        self.client.login(username="tg_a1", password=PASSWORD)
        response = self.client.get(self.preview_url)
        self.assertEqual(response.status_code, 200)

    def test_send_requires_post_not_get(self):
        User.objects.create_user(username="tg_a2", password=PASSWORD, is_staff=True, is_superuser=True)
        self.client.login(username="tg_a2", password=PASSWORD)
        response = self.client.get(self.send_url)
        self.assertEqual(response.status_code, 405)

    def test_admin_send_enqueues_manual_non_forced_by_default(self):
        User.objects.create_user(username="tg_a3", password=PASSWORD, is_staff=True, is_superuser=True)
        self.client.login(username="tg_a3", password=PASSWORD)
        with mock.patch("notifications.tasks.send_ml_daily_report.delay") as mock_delay:
            response = self.client.post(self.send_url)
        self.assertEqual(response.status_code, 302)
        mock_delay.assert_called_once_with(manual=True, force=False)

    def test_force_resend_is_audited(self):
        User.objects.create_user(username="tg_a4", password=PASSWORD, is_staff=True, is_superuser=True)
        self.client.login(username="tg_a4", password=PASSWORD)
        with mock.patch("notifications.tasks.send_ml_daily_report.delay") as mock_delay:
            self.client.post(self.send_url, {"force": "yes"})
        mock_delay.assert_called_once_with(manual=True, force=True)
        self.assertTrue(AdminAuditLog.objects.filter(action=AdminAuditAction.TELEGRAM_REPORT_FORCED).exists())

    def test_recipient_chat_id_from_request_body_is_ignored(self):
        """The send view must never read a chat id from the request — the
        task always reads settings.TELEGRAM_ADMIN_CHAT_ID itself."""
        User.objects.create_user(username="tg_a5", password=PASSWORD, is_staff=True, is_superuser=True)
        self.client.login(username="tg_a5", password=PASSWORD)
        with mock.patch("notifications.tasks.send_ml_daily_report.delay") as mock_delay:
            self.client.post(self.send_url, {"chat_id": "attacker-controlled-id"})
        mock_delay.assert_called_once_with(manual=True, force=False)


# ---------------------------------------------------------------------------
# Telegram ML daily report — repeated-failure operational alert
# ---------------------------------------------------------------------------


class TelegramReportRepeatedFailureAlertTests(TestCase):
    def _make_delivery(self, report_date, status):
        return MlDailyReportDelivery.objects.create(
            report_date=report_date,
            recipient_masked="12***78",
            idempotency_key=f"telegram_ml_daily_report:test:{report_date.isoformat()}",
            status=status,
        )

    def test_no_alert_with_fewer_than_three_deliveries(self):
        from market.services.ops_alerts import _telegram_report_repeated_failure_alert

        self._make_delivery(timezone.localdate(), MlDailyReportStatus.FAILED)
        self.assertEqual(_telegram_report_repeated_failure_alert(), [])

    def test_no_alert_when_most_recent_succeeded(self):
        from datetime import timedelta

        from market.services.ops_alerts import _telegram_report_repeated_failure_alert

        today = timezone.localdate()
        self._make_delivery(today - timedelta(days=2), MlDailyReportStatus.FAILED)
        self._make_delivery(today - timedelta(days=1), MlDailyReportStatus.FAILED)
        self._make_delivery(today, MlDailyReportStatus.SENT)
        self.assertEqual(_telegram_report_repeated_failure_alert(), [])

    def test_alert_fires_after_three_consecutive_failures(self):
        from datetime import timedelta

        from market.services.ops_alerts import _telegram_report_repeated_failure_alert

        today = timezone.localdate()
        for i in range(3):
            self._make_delivery(today - timedelta(days=i), MlDailyReportStatus.FAILED)
        alerts = _telegram_report_repeated_failure_alert()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["key"], "telegram_ml_report_repeated_failure")
        self.assertEqual(alerts[0]["severity"], "critical")
