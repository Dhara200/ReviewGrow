import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask, request
from werkzeug.security import generate_password_hash

from app.routes.auth import auth_bp, get_client_ip
from app.services.trusted_proxy_service import TrustedProxyFix


class FakeCursor:
    def __init__(self, user=None, business=None):
        self.user = user
        self.business = business
        self.query = ""

    def execute(self, query, params):
        self.query = query

    def fetchone(self):
        return self.business if "FROM businesses" in self.query else self.user

    def close(self):
        pass


class FakeConnection:
    def __init__(self, user=None, business=None):
        self.user = user
        self.business = business

    def cursor(self, dictionary=False):
        return FakeCursor(self.user, self.business)

    def close(self):
        pass


class LoginProxySecurityTests(unittest.TestCase):
    def proxy_app(self, trusted=("10.0.0.10",)):
        app = Flask(__name__)

        @app.get("/ip")
        def ip():
            return {"ip": get_client_ip(), "remote_addr": request.remote_addr}

        app.wsgi_app = TrustedProxyFix(app.wsgi_app, trusted)
        return app

    def test_direct_peer_cannot_control_identity_with_forwarded_header(self):
        client = self.proxy_app().test_client()
        response = client.get(
            "/ip", environ_base={"REMOTE_ADDR": "198.51.100.20"},
            headers={"X-Forwarded-For": "203.0.113.99"},
        )
        self.assertEqual("198.51.100.20", response.get_json()["ip"])

    def test_exactly_one_trusted_proxy_hop_resolves_client_address(self):
        client = self.proxy_app().test_client()
        response = client.get(
            "/ip", environ_base={"REMOTE_ADDR": "10.0.0.10"},
            headers={"X-Forwarded-For": "203.0.113.7"},
        )
        self.assertEqual("203.0.113.7", response.get_json()["ip"])

    def login_app(self):
        app = Flask(__name__, template_folder="../app/templates")
        app.config.update(TESTING=True, SECRET_KEY="proxy-test")
        app.register_blueprint(auth_bp)
        app.wsgi_app = TrustedProxyFix(app.wsgi_app, ("10.0.0.10",))
        return app

    @patch("app.routes.auth.verify_recaptcha", return_value=SimpleNamespace(success=True))
    def test_rotating_forged_headers_do_not_bypass_threshold(self, recaptcha):
        attempts = {}

        def locked(email, ip):
            row = attempts.get((email, ip))
            return row.get("locked_until") if row else None

        def failed(email, ip):
            row = attempts.setdefault((email, ip), {"count": 0, "locked_until": None})
            row["count"] += 1
            if row["count"] >= 5:
                row["locked_until"] = datetime.utcnow() + timedelta(minutes=15)
            return row["locked_until"]

        with patch("app.routes.auth.is_login_locked", side_effect=locked), \
             patch("app.routes.auth.record_failed_login", side_effect=failed), \
             patch("app.routes.auth.get_connection", return_value=FakeConnection()):
            client = self.login_app().test_client()
            statuses = []
            for number in range(6):
                response = client.post(
                    "/login-page",
                    data={"email": " User@Example.com ", "password": "wrong"},
                    environ_base={"REMOTE_ADDR": "198.51.100.20"},
                    headers={"X-Forwarded-For": f"203.0.113.{number + 1}"},
                )
                statuses.append(response.status_code)
        self.assertEqual([401, 401, 401, 401, 401, 429], statuses)
        self.assertEqual({("user@example.com", "198.51.100.20")}, set(attempts))

    @patch("app.routes.auth.verify_recaptcha", return_value=SimpleNamespace(success=True))
    @patch("app.routes.auth.has_active_subscription", return_value=True)
    @patch("app.routes.auth.reset_failed_login")
    @patch("app.routes.auth.is_login_locked", return_value=None)
    def test_legitimate_login_resets_failure_state(self, locked, reset, active, recaptcha):
        user = {
            "id": 7, "name": "Owner", "email": "owner@example.com",
            "password_hash": generate_password_hash("correct-password"), "role": "owner",
        }
        with patch(
            "app.routes.auth.get_connection",
            side_effect=[FakeConnection(user=user), FakeConnection(user=user, business={"id": 3})],
        ):
            response = self.login_app().test_client().post(
                "/login-page", data={"email": " OWNER@example.com ", "password": "correct-password"},
                environ_base={"REMOTE_ADDR": "198.51.100.20"},
                headers={"X-Forwarded-For": "203.0.113.200"},
            )
        self.assertEqual(302, response.status_code)
        reset.assert_called_once_with("owner@example.com", "198.51.100.20")

    @patch("app.routes.auth.verify_recaptcha", return_value=SimpleNamespace(success=True))
    @patch("app.routes.auth.record_failed_login")
    @patch("app.routes.auth.is_login_locked", return_value=None)
    def test_expired_lock_allows_a_new_failed_attempt(self, locked, failed, recaptcha):
        with patch("app.routes.auth.get_connection", return_value=FakeConnection()):
            response = self.login_app().test_client().post(
                "/login-page", data={"email": "user@example.com", "password": "wrong"},
                environ_base={"REMOTE_ADDR": "198.51.100.20"},
            )
        self.assertEqual(401, response.status_code)
        failed.assert_called_once_with("user@example.com", "198.51.100.20")

    def test_missing_or_invalid_remote_address_is_bounded(self):
        client = self.proxy_app().test_client()
        response = client.get("/ip", environ_base={"REMOTE_ADDR": "not-an-ip"})
        self.assertEqual("unknown", response.get_json()["ip"])
        self.assertLessEqual(len(response.get_json()["ip"]), 100)


if __name__ == "__main__":
    unittest.main()
