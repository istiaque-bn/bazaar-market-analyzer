"""Phase 9 — administrative audit records for staff-triggered pipelines
and model activation."""
from datetime import date
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from market.models import AdminAuditAction, AdminAuditLog, MLModelVersion


def _login_staff(client, username, *, superuser=False):
    if superuser:
        user = User.objects.create_superuser(username=username, password="Correct-Horse-Battery-Staple-42")
    else:
        user = User.objects.create_user(username=username, password="Correct-Horse-Battery-Staple-42")
        user.is_staff = True
        user.save(update_fields=["is_staff"])
    client.login(username=username, password="Correct-Horse-Battery-Staple-42")
    return user


class PipelineTriggerAuditTests(TestCase):
    """Pipeline triggering is Admin-only (see market.views.run_pipeline_view
    / accounts.decorators.admin_required) — these use a superuser login,
    not plain Staff, which now gets 403 before the view (and its audit
    write) ever runs — see market.tests.test_pipeline_auth."""

    @mock.patch("market.tasks.run_full_analysis_task.delay")
    def test_successful_trigger_is_audited_with_user_and_mode(self, mock_delay):
        user = _login_staff(self.client, "admin_trigger", superuser=True)
        self.client.post(reverse("run_pipeline"), {"mode": "analyze"})

        log = AdminAuditLog.objects.get(action=AdminAuditAction.PIPELINE_TRIGGERED)
        self.assertEqual(log.user_id, user.id)
        self.assertEqual(log.username_snapshot, "admin_trigger")
        self.assertEqual(log.detail.get("mode"), "analyze")
        self.assertNotIn("error", log.detail)

    @mock.patch("market.tasks.run_full_analysis_task.delay")
    def test_failed_trigger_is_still_audited_with_error_detail(self, mock_delay):
        mock_delay.side_effect = RuntimeError("broker unreachable")
        _login_staff(self.client, "admin_trigger_err", superuser=True)
        self.client.post(reverse("run_pipeline"), {"mode": "analyze"})

        log = AdminAuditLog.objects.get(action=AdminAuditAction.PIPELINE_TRIGGERED)
        self.assertIn("broker unreachable", log.detail.get("error", ""))

    def test_anonymous_trigger_attempt_creates_no_audit_row(self):
        self.client.post(reverse("run_pipeline"), {"mode": "analyze"})
        self.assertEqual(AdminAuditLog.objects.count(), 0)


class ModelActivationAdminActionTests(TestCase):
    def setUp(self):
        self.user = _login_staff(self.client, "modeladmin", superuser=True)
        self.active_version = MLModelVersion.objects.create(
            model_name="forward_return_rf",
            version="20260101-000000",
            exchange_scope="DSE",
            is_active=True,
            data_cutoff=date(2026, 1, 1),
        )
        self.inactive_version = MLModelVersion.objects.create(
            model_name="forward_return_rf",
            version="20260102-000000",
            exchange_scope="DSE",
            is_active=False,
            data_cutoff=date(2026, 1, 2),
        )
        self.changelist_url = reverse("admin:market_mlmodelversion_changelist")

    def test_deactivate_action_flips_flag_and_writes_audit_row(self):
        response = self.client.post(
            self.changelist_url,
            {"action": "deactivate_model_versions", "_selected_action": [str(self.active_version.pk)]},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.active_version.refresh_from_db()
        self.assertFalse(self.active_version.is_active)

        log = AdminAuditLog.objects.get(action=AdminAuditAction.MODEL_DEACTIVATED)
        self.assertEqual(log.username_snapshot, "modeladmin")
        self.assertEqual(log.detail["version"], "20260101-000000")

    def test_deactivate_action_on_already_inactive_row_is_a_no_op(self):
        self.client.post(
            self.changelist_url,
            {"action": "deactivate_model_versions", "_selected_action": [str(self.inactive_version.pk)]},
            follow=True,
        )
        self.assertEqual(AdminAuditLog.objects.filter(action=AdminAuditAction.MODEL_DEACTIVATED).count(), 0)

    def test_reactivate_action_flips_flag_and_writes_audit_row(self):
        self.client.post(
            self.changelist_url,
            {"action": "reactivate_model_versions", "_selected_action": [str(self.inactive_version.pk)]},
            follow=True,
        )
        self.inactive_version.refresh_from_db()
        self.assertTrue(self.inactive_version.is_active)

        log = AdminAuditLog.objects.get(action=AdminAuditAction.MODEL_ACTIVATED)
        self.assertEqual(log.detail["version"], "20260102-000000")

    def test_audit_log_survives_user_deletion(self):
        self.client.post(
            self.changelist_url,
            {"action": "deactivate_model_versions", "_selected_action": [str(self.active_version.pk)]},
            follow=True,
        )
        log = AdminAuditLog.objects.get(action=AdminAuditAction.MODEL_DEACTIVATED)
        self.user.delete()
        log.refresh_from_db()
        self.assertIsNone(log.user_id)
        self.assertEqual(log.username_snapshot, "modeladmin")  # snapshot survives the FK going null


class AdminAuditLogAdminPermissionTests(TestCase):
    def test_audit_log_is_read_only_in_admin(self):
        _login_staff(self.client, "auditor", superuser=True)
        AdminAuditLog.objects.create(action=AdminAuditAction.PIPELINE_TRIGGERED, detail={"mode": "analyze"})

        add_url = reverse("admin:market_adminauditlog_add")
        self.assertEqual(self.client.get(add_url).status_code, 403)

        log = AdminAuditLog.objects.first()
        change_url = reverse("admin:market_adminauditlog_change", args=[log.pk])
        # A superuser can still *view* the read-only detail (Django grants
        # view access via has_view_permission's default OR-with-change
        # even though has_change_permission is False) — but actually
        # attempting to save an edit must be rejected.
        self.assertEqual(self.client.get(change_url).status_code, 200)
        response = self.client.post(change_url, {"action": AdminAuditAction.MODEL_ACTIVATED})
        self.assertEqual(response.status_code, 403)
        log.refresh_from_db()
        self.assertEqual(log.action, AdminAuditAction.PIPELINE_TRIGGERED)  # unchanged

        delete_url = reverse("admin:market_adminauditlog_delete", args=[log.pk])
        response = self.client.post(delete_url, {"post": "yes"})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(AdminAuditLog.objects.filter(pk=log.pk).exists())
