import unittest
import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask, request
from werkzeug.security import generate_password_hash

from app.routes.auth import auth_bp, get_client_ip
from app.services.trusted_proxy_service import TrustedProxyFix
from app.services.limiter_service import LimitStatus
from app.services.csrf_service import init_csrf
from app.config import Config


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
        app.config.update(
            TESTING=True, SECRET_KEY="proxy-test",
            LOGIN_DUMMY_PASSWORD_HASH=Config.LOGIN_DUMMY_PASSWORD_HASH,
        )
        init_csrf(app)
        app.register_blueprint(auth_bp)
        app.wsgi_app = TrustedProxyFix(app.wsgi_app, ("10.0.0.10",))
        return app

    def login_token(self, client):
        page = client.get("/login-page").get_data(as_text=True)
        return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)

    def unblocked_limiter(self):
        limiter = MagicMock()
        limiter.check_ip.return_value = LimitStatus(False, 0, 0)
        limiter.check_account_and_pair.return_value = (
            LimitStatus(False, 0, 0), LimitStatus(False, 0, 0)
        )
        return limiter

    @patch("app.routes.auth.verify_recaptcha", return_value=SimpleNamespace(success=True))
    def test_rotating_forged_headers_do_not_bypass_threshold(self, recaptcha):
        limiter = self.unblocked_limiter()
        pair_count = {"value": 0}
        def check_account_and_pair(email, ip):
            blocked = pair_count["value"] >= 5
            return LimitStatus(False, pair_count["value"], 0), LimitStatus(
                blocked, pair_count["value"], 900 if blocked else 0
            )
        def record_failure(email, ip):
            pair_count["value"] += 1
        limiter.check_account_and_pair.side_effect = check_account_and_pair
        limiter.record_failure.side_effect = record_failure
        with patch("app.routes.auth._get_login_limiter", return_value=limiter), \
             patch("app.routes.auth.get_connection", return_value=FakeConnection()):
            client = self.login_app().test_client()
            token = self.login_token(client)
            statuses = []
            for number in range(6):
                response = client.post(
                    "/login-page",
                    data={"email": " User@Example.com ", "password": "wrong", "csrf_token": token},
                    environ_base={"REMOTE_ADDR": "198.51.100.20"},
                    headers={"X-Forwarded-For": f"203.0.113.{number + 1}"},
                )
                statuses.append(response.status_code)
        self.assertEqual([401, 401, 401, 401, 401, 429], statuses)
        self.assertEqual(
            {("user@example.com", "198.51.100.20")},
            set(call.args for call in limiter.record_failure.call_args_list),
        )

    @patch("app.routes.auth.verify_recaptcha", return_value=SimpleNamespace(success=True))
    @patch("app.routes.auth.has_active_subscription", return_value=True)
    def test_legitimate_login_resets_failure_state(self, active, recaptcha):
        user = {
            "id": 7, "name": "Owner", "email": "owner@example.com",
            "password_hash": generate_password_hash("correct-password"), "role": "owner",
        }
        limiter = self.unblocked_limiter()
        with patch("app.routes.auth._get_login_limiter", return_value=limiter), patch(
            "app.routes.auth.get_connection",
            side_effect=[FakeConnection(user=user), FakeConnection(user=user, business={"id": 3})],
        ):
            client = self.login_app().test_client()
            token = self.login_token(client)
            with client.session_transaction() as pre_auth_session:
                pre_auth_session["stale_value"] = "must-not-survive"
                pre_auth_session["role"] = "admin"
            response = client.post(
                "/login-page", data={"email": " OWNER@example.com ", "password": "correct-password", "csrf_token": token},
                environ_base={"REMOTE_ADDR": "198.51.100.20"},
                headers={"X-Forwarded-For": "203.0.113.200"},
            )
        self.assertEqual(302, response.status_code)
        limiter.reset_after_success.assert_called_once_with(
            "owner@example.com", "198.51.100.20"
        )
        with client.session_transaction() as authenticated_session:
            self.assertTrue(authenticated_session.permanent)
            self.assertEqual(7, authenticated_session["user_id"])
            self.assertEqual("owner", authenticated_session["role"])
            self.assertNotIn("stale_value", authenticated_session)

    @patch("app.routes.auth.verify_recaptcha", return_value=SimpleNamespace(success=True))
    def test_expired_lock_allows_a_new_failed_attempt(self, recaptcha):
        limiter = self.unblocked_limiter()
        with patch("app.routes.auth._get_login_limiter", return_value=limiter), patch("app.routes.auth.get_connection", return_value=FakeConnection()):
            client = self.login_app().test_client()
            token = self.login_token(client)
            response = client.post(
                "/login-page", data={
                    "email": "user@example.com", "password": "wrong",
                    "csrf_token": token,
                },
                environ_base={"REMOTE_ADDR": "198.51.100.20"},
            )
        self.assertEqual(401, response.status_code)
        limiter.record_failure.assert_called_once_with(
            "user@example.com", "198.51.100.20"
        )

    def test_missing_or_invalid_remote_address_is_bounded(self):
        client = self.proxy_app().test_client()
        response = client.get("/ip", environ_base={"REMOTE_ADDR": "not-an-ip"})
        self.assertEqual("unknown", response.get_json()["ip"])
        self.assertLessEqual(len(response.get_json()["ip"]), 100)


if __name__ == "__main__":
    unittest.main()
