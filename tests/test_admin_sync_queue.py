import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from flask import Flask

from app.routes.admin import admin_bp
from app.services.admin_sync_queue_service import (
    AdminSyncQueueService,
    format_duration,
    normalize_sync_queue_filters,
    sanitize_sync_job_error,
)
from app.services.csrf_service import init_csrf


class FakeCursor:
    def __init__(self, fetchone=None, fetchall=None, error=None):
        self.fetchone_result = fetchone
        self.fetchall_result = list(fetchall or [])
        self.error = error
        self.executions = []
        self.closed = False

    def execute(self, query, params=()):
        self.executions.append((" ".join(query.split()), params))
        if self.error:
            raise self.error

    def fetchone(self):
        return self.fetchone_result

    def fetchall(self):
        return self.fetchall_result

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, cursor):
        self.cursor_value = cursor
        self.closed = False

    def cursor(self, dictionary=False):
        return self.cursor_value

    def close(self):
        self.closed = True


def service_for(cursor):
    connection = FakeConnection(cursor)
    return AdminSyncQueueService(lambda: connection), connection


class AdminSyncQueueServiceTests(unittest.TestCase):
    def test_summary_normalizes_counts_duration_and_empty_values(self):
        cursor = FakeCursor(fetchone={
            "pending_jobs": 5,
            "running_jobs": 2,
            "completed_today": 7,
            "failed_today": 3,
            "average_sync_seconds": 90.5,
            "oldest_pending_seconds": 181,
        })
        service, connection = service_for(cursor)

        summary = service.get_sync_queue_summary()

        self.assertEqual(5, summary["pending_jobs"])
        self.assertEqual(7, summary["completed_today"])
        self.assertEqual(3, summary["failed_today"])
        self.assertEqual(90.5, summary["average_sync_seconds"])
        self.assertEqual(181, summary["oldest_pending_seconds"])
        query = cursor.executions[0][0]
        self.assertIn("DATE(completed_at)=UTC_DATE()", query)
        self.assertIn("INTERVAL 24 HOUR", query)
        self.assertIn("TIMESTAMPDIFF(MICROSECOND, started_at, completed_at)", query)
        self.assertTrue(cursor.closed)
        self.assertTrue(connection.closed)

    def test_empty_summary_uses_safe_zero_and_na_values(self):
        service, _connection = service_for(FakeCursor(fetchone={}))
        summary = service.get_sync_queue_summary()
        self.assertEqual(0, summary["pending_jobs"])
        self.assertEqual(0, summary["running_jobs"])
        self.assertIsNone(summary["average_sync_seconds"])
        self.assertIsNone(summary["oldest_pending_seconds"])
        self.assertEqual("N/A", format_duration(None))
        self.assertEqual("None", format_duration(None, "None"))

    def test_recent_jobs_use_joined_parameterized_filtered_query_and_safe_errors(self):
        cursor = FakeCursor(fetchall=[{
            "id": 41,
            "business_id": 9,
            "business_name": "Tenant Business",
            "user_id": 7,
            "user_name": "Owner",
            "user_email": "owner@example.test",
            "status": "failed",
            "error_message": "Bearer secret-token " + ("x" * 300),
            "duration_seconds": 12,
        }])
        service, _connection = service_for(cursor)

        jobs = service.get_recent_sync_jobs("failed", 9, "7d")

        query, params = cursor.executions[0]
        self.assertIn("JOIN businesses b ON b.id=j.business_id", query)
        self.assertIn("JOIN users u ON u.id=j.user_id", query)
        self.assertIn("j.status=%s", query)
        self.assertIn("j.business_id=%s", query)
        self.assertIn("INTERVAL 7 DAY", query)
        self.assertIn("LIMIT %s", query)
        self.assertEqual(("failed", 9, 50), params)
        self.assertNotIn("error_message", jobs[0])
        self.assertNotIn("secret-token", jobs[0]["safe_error"])
        self.assertLessEqual(len(jobs[0]["safe_error"]), 160)
        self.assertEqual("Tenant Business", jobs[0]["business_name"])
        self.assertIsNone(jobs[0]["attempt_count"])

    def test_health_rules_include_expired_lease_and_stale_heartbeat(self):
        cursor = FakeCursor(fetchone={"expired_leases": 1, "stale_heartbeats": 2})
        service, _connection = service_for(cursor)
        summary = {
            "pending_jobs": 5,
            "running_jobs": 2,
            "completed_today": 0,
            "failed_today": 3,
            "average_sync_seconds": None,
            "oldest_pending_seconds": 121,
        }

        health = service.get_sync_queue_health(summary)

        self.assertEqual(5, len(health["warnings"]))
        self.assertEqual(1, health["expired_leases"])
        self.assertEqual(2, health["stale_heartbeats"])
        query, params = cursor.executions[0]
        self.assertIn("lease_expires_at < UTC_TIMESTAMP(6)", query)
        self.assertIn("heartbeat_at IS NULL", query)
        self.assertEqual((health["stale_threshold_seconds"],), params)

    def test_filter_allowlists_reject_invalid_values(self):
        self.assertEqual(
            (None, None, "24h"),
            normalize_sync_queue_filters("deleted", "not-an-id", "forever"),
        )
        self.assertEqual(
            ("processing", 9, "today"),
            normalize_sync_queue_filters("processing", "9", "today"),
        )

    def test_resources_close_when_query_raises(self):
        cursor = FakeCursor(error=RuntimeError("database unavailable"))
        service, connection = service_for(cursor)
        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            service.get_sync_queue_summary()
        self.assertTrue(cursor.closed)
        self.assertTrue(connection.closed)

    def test_error_sanitization_and_html_are_safe(self):
        message = sanitize_sync_job_error(
            '<script>alert(1)</script> access_token=secret password=hunter2'
        )
        self.assertNotIn("secret", message)
        self.assertNotIn("hunter2", message)
        self.assertIn("<script>", message)


class AdminSyncQueueRouteTests(unittest.TestCase):
    def setUp(self):
        template_folder = Path(__file__).resolve().parents[1] / "app" / "templates"
        app = Flask(__name__, template_folder=str(template_folder))
        app.config.update(TESTING=True, SECRET_KEY="sync-queue-test-secret")
        init_csrf(app)
        app.register_blueprint(admin_bp)
        self.client = app.test_client()
        self.monitor = MagicMock()
        self.monitor.get_sync_queue_summary.return_value = {
            "pending_jobs": 0,
            "running_jobs": 0,
            "completed_today": 0,
            "failed_today": 0,
            "average_sync_seconds": None,
            "oldest_pending_seconds": None,
        }
        self.monitor.get_sync_queue_health.return_value = {
            "warnings": [], "expired_leases": 0, "stale_heartbeats": 0,
            "stale_threshold_seconds": 60,
        }
        self.monitor.get_recent_sync_jobs.return_value = []
        self.monitor.get_business_options.return_value = []
        patcher = patch("app.routes.admin.sync_queue_monitor", self.monitor)
        patcher.start()
        self.addCleanup(patcher.stop)

    def login(self, role):
        with self.client.session_transaction() as session:
            session["user_id"] = 7
            session["role"] = role

    def test_admin_can_access_and_filters_are_server_side(self):
        self.login("admin")
        response = self.client.get(
            "/admin/sync-queue?status=failed&business=9&date_range=7d"
        )
        self.assertEqual(200, response.status_code)
        self.assertIn(b"Sync Queue", response.data)
        self.assertIn(b"No synchronization jobs match", response.data)
        self.monitor.get_recent_sync_jobs.assert_called_once_with(
            status="failed", business_id=9, date_range="7d"
        )

    def test_unauthenticated_and_non_admin_access_are_denied(self):
        response = self.client.get("/admin/sync-queue")
        self.assertEqual(302, response.status_code)
        self.assertTrue(response.location.endswith("/login-page"))

        self.login("owner")
        response = self.client.get("/admin/sync-queue")
        self.assertEqual(403, response.status_code)
        self.assertNotIn(b"Sync Queue", response.data)
        self.monitor.get_sync_queue_summary.assert_not_called()

    def test_rendered_error_is_escaped_and_contains_no_token_fields(self):
        self.login("admin")
        self.monitor.get_recent_sync_jobs.return_value = [{
            "id": 1, "business_id": 9, "business_name": "Business",
            "user_id": 7, "user_name": "Owner", "user_email": "o@example.test",
            "status": "failed", "attempt_count": None, "worker_id": None,
            "created_at": None, "started_at": None, "completed_at": None,
            "duration_seconds": None, "safe_error": "<script>alert(1)</script>",
        }]
        response = self.client.get("/admin/sync-queue")
        self.assertNotIn(b"<script>alert(1)</script>", response.data)
        self.assertIn(b"&lt;script&gt;", response.data)
        for forbidden in (b"access_token", b"refresh_token", b"client_secret"):
            self.assertNotIn(forbidden, response.data)

    def test_sidebar_contains_active_admin_sync_queue_item(self):
        self.login("admin")
        response = self.client.get("/admin/sync-queue")
        self.assertIn(b'href="/admin/sync-queue" class="rs-nav-link active"', response.data)


if __name__ == "__main__":
    unittest.main()
