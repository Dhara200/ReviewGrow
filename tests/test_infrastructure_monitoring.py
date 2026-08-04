import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from flask import Flask

from app.routes.admin import admin_bp
from app.services.csrf_service import init_csrf
from app.services.infrastructure_monitoring_service import (
    EC2DockerInfrastructureProvider,
    get_infrastructure_status,
)


def snapshot():
    return {
        "overall_status": "healthy", "refreshed_at_ist": "2026-08-04T12:00:00+05:30",
        "application": {"status": "healthy", "environment": "testing", "version": "test",
                        "uptime_seconds": 10, "timestamp_ist": "now", "health_check": {"status": "healthy"}},
        "host": {"cpu_percent": 1, "load_average": {"1m": 0, "5m": 0, "15m": 0},
                 "memory": {"percent": 2}, "swap": {"percent": 0}, "disk": {"percent": 3}},
        "docker": {"available": False, "reason": "Unavailable", "containers": {}},
        "gunicorn": {"configured_workers": 2, "running_workers": 2},
        "background_workers": {"configured_workers": 1, "running_workers": 1},
        "database": {"reachable": True}, "jobs": {"available": True}, "alerts": [],
    }


class InfrastructureRouteTests(unittest.TestCase):
    def setUp(self):
        template_folder = Path(__file__).resolve().parents[1] / "app" / "templates"
        static_folder = Path(__file__).resolve().parents[1] / "app" / "static"
        app = Flask(__name__, template_folder=str(template_folder), static_folder=str(static_folder))
        app.config.update(TESTING=True, SECRET_KEY="infra-test")
        init_csrf(app); app.register_blueprint(admin_bp)
        self.client = app.test_client()
        self.patcher = patch("app.routes.admin.get_infrastructure_status", return_value=snapshot())
        self.monitor = self.patcher.start(); self.addCleanup(self.patcher.stop)

    def login(self, role):
        with self.client.session_transaction() as session:
            session["user_id"] = 7; session["role"] = role

    def test_admin_access_and_template_renders_unavailable_metrics(self):
        self.login("admin")
        response = self.client.get("/admin/infra")
        self.assertEqual(200, response.status_code)
        self.assertIn(b"Infrastructure", response.data)
        self.assertIn(b'href="/admin/infra" class="rs-nav-link active"', response.data)
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_normal_and_anonymous_users_are_denied(self):
        self.login("owner")
        self.assertEqual(403, self.client.get("/admin/infra").status_code)
        self.assertEqual(403, self.client.get("/admin/api/infra/status").status_code)
        with self.client.session_transaction() as session: session.clear()
        self.assertEqual(302, self.client.get("/admin/infra").status_code)
        self.assertEqual(302, self.client.get("/admin/api/infra/status").status_code)
        self.monitor.assert_not_called()

    def test_status_endpoint_structure_and_no_cache(self):
        self.login("admin")
        response = self.client.get("/admin/api/infra/status")
        self.assertEqual(200, response.status_code)
        self.assertEqual(set(snapshot()), set(response.get_json()))
        self.assertIn("no-store", response.headers["Cache-Control"])


class InfrastructureProviderTests(unittest.TestCase):
    def test_collector_failure_isolation_and_database_unavailable(self):
        provider = EC2DockerInfrastructureProvider()
        provider.collect_application_metrics = MagicMock(side_effect=RuntimeError("token=secret"))
        provider.collect_host_metrics = MagicMock(return_value=provider._host_unavailable())
        provider.collect_docker_metrics = MagicMock(return_value=provider._docker_unavailable())
        provider.collect_gunicorn_metrics = MagicMock(return_value=provider._gunicorn_unavailable())
        provider.collect_worker_metrics = MagicMock(return_value=provider._worker_unavailable())
        provider.collect_database_metrics = MagicMock(return_value=provider._database_unavailable())
        provider.collect_job_metrics = MagicMock(return_value=provider._jobs_unavailable())
        result = provider.collect()
        self.assertEqual("unavailable", result["application"]["status"])
        self.assertTrue(any(a["code"] == "database_unavailable" for a in result["alerts"]))

    def test_alert_thresholds_and_worker_mismatch(self):
        provider = EC2DockerInfrastructureProvider(); data = snapshot()
        data["host"].update(cpu_percent=76, memory={"percent": 81}, disk={"percent": 82}, swap={"percent": 21})
        data["background_workers"] = {"configured_workers": 2, "running_workers": 1}
        data["jobs"] = {"pending": 101, "oldest_pending_age_seconds": 901}
        codes = {a["code"] for a in provider.build_alerts(data)}
        self.assertTrue({"cpu_high", "memory_high", "disk_high", "swap_used", "background_workers_mismatch", "queue_stale", "queue_depth"} <= codes)

    def test_unavailable_docker_does_not_create_unhealthy_alert(self):
        provider = EC2DockerInfrastructureProvider(); data = snapshot()
        data["docker"] = provider._docker_unavailable()
        self.assertFalse(any(a["code"].startswith("container_") for a in provider.build_alerts(data)))

    def test_output_never_contains_secret_values_or_sensitive_keys(self):
        provider = MagicMock(); provider.collect.return_value = snapshot()
        result = get_infrastructure_status(provider=provider, use_cache=False)
        encoded = json.dumps(result).lower()
        for forbidden in ("password", "api_key", "access_token", "client_secret", "internal_ip", "cmdline", "environment_variables"):
            self.assertNotIn(forbidden, encoded)

    def test_job_queries_are_aggregate_and_have_no_row_fetches(self):
        cursor = MagicMock(); cursor.fetchone.return_value = {}
        connection = MagicMock(); connection.cursor.return_value = cursor
        with patch("app.services.infrastructure_monitoring_service.get_connection", return_value=connection):
            EC2DockerInfrastructureProvider().collect_job_metrics()
        self.assertEqual(4, cursor.execute.call_count)
        self.assertTrue(all("SELECT" in call.args[0] and "SUM(" in call.args[0] for call in cursor.execute.call_args_list))
        self.assertTrue(all("SELECT *" not in call.args[0].upper() for call in cursor.execute.call_args_list))


if __name__ == "__main__":
    unittest.main()
