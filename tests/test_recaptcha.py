import unittest
import time
from unittest.mock import Mock, patch

import requests
from flask import Flask

from app.routes.auth import auth_bp
from app.services.recaptcha_service import verify_recaptcha
from app.services.csrf_service import (
    REGISTRATION_CSRF_ISSUED_AT_KEY,
    REGISTRATION_CSRF_SESSION_KEY,
    init_csrf,
)


class RecaptchaServiceTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            APP_ENV="testing",
            RECAPTCHA_ENABLED=True,
            RECAPTCHA_SITE_KEY="test-site-key",
            RECAPTCHA_SECRET_KEY="test-secret",
            RECAPTCHA_VERIFY_URL="https://example.test/siteverify",
            RECAPTCHA_SCORE_THRESHOLD=0.5,
            RECAPTCHA_TIMEOUT_SECONDS=2,
        )
        self.context = self.app.app_context()
        self.context.push()

    def tearDown(self):
        self.context.pop()

    def _response(self, payload):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = payload
        return response

    @patch("app.services.recaptcha_service.requests.post")
    def test_valid_response_and_remote_ip(self, post):
        post.return_value = self._response(
            {"success": True, "action": "login", "score": 0.8}
        )

        result = verify_recaptcha("token", "login", "203.0.113.1")

        self.assertTrue(result.success)
        sent_data = post.call_args.kwargs["data"]
        self.assertEqual(sent_data["remoteip"], "203.0.113.1")
        self.assertEqual(post.call_args.kwargs["timeout"], 2)

    @patch("app.services.recaptcha_service.requests.post")
    def test_missing_token_is_rejected_without_provider_call(self, post):
        result = verify_recaptcha("", "login")
        self.assertFalse(result.success)
        self.assertEqual(result.reason, "missing_token")
        post.assert_not_called()

    @patch("app.services.recaptcha_service.requests.post")
    def test_invalid_token_is_rejected(self, post):
        post.return_value = self._response({"success": False})
        result = verify_recaptcha("token", "login")
        self.assertEqual(result.reason, "invalid_token")

    @patch("app.services.recaptcha_service.requests.post")
    def test_http_failure_is_rejected(self, post):
        response = Mock()
        response.raise_for_status.side_effect = requests.HTTPError("failed")
        post.return_value = response
        result = verify_recaptcha("token", "login")
        self.assertEqual(result.reason, "provider_error")

    @patch("app.services.recaptcha_service.requests.post")
    def test_action_mismatch_is_rejected(self, post):
        post.return_value = self._response(
            {"success": True, "action": "register", "score": 0.9}
        )
        result = verify_recaptcha("token", "login")
        self.assertEqual(result.reason, "action_mismatch")

    @patch("app.services.recaptcha_service.requests.post")
    def test_score_below_threshold_is_rejected(self, post):
        post.return_value = self._response(
            {"success": True, "action": "login", "score": 0.49}
        )
        result = verify_recaptcha("token", "login")
        self.assertEqual(result.reason, "low_score")

    @patch("app.services.recaptcha_service.requests.post")
    def test_score_equal_to_threshold_is_accepted(self, post):
        post.return_value = self._response(
            {"success": True, "action": "login", "score": 0.5}
        )
        self.assertTrue(verify_recaptcha("token", "login").success)

    @patch("app.services.recaptcha_service.requests.post")
    def test_score_above_threshold_is_accepted(self, post):
        post.return_value = self._response(
            {"success": True, "action": "login", "score": 0.51}
        )
        self.assertTrue(verify_recaptcha("token", "login").success)

    @patch("app.services.recaptcha_service.requests.post")
    def test_timeout_is_rejected(self, post):
        post.side_effect = requests.Timeout("timeout")
        result = verify_recaptcha("token", "login")
        self.assertEqual(result.reason, "provider_error")

    @patch("app.services.recaptcha_service.requests.post")
    def test_malformed_json_is_rejected(self, post):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.side_effect = ValueError("bad json")
        post.return_value = response
        result = verify_recaptcha("token", "login")
        self.assertEqual(result.reason, "provider_error")

    def test_disabled_in_development_bypasses_verification(self):
        self.app.config.update(RECAPTCHA_ENABLED=False, APP_ENV="development")
        self.assertTrue(verify_recaptcha(None, "login").success)

    def test_disabled_in_production_fails_closed(self):
        self.app.config.update(
            TESTING=False,
            RECAPTCHA_ENABLED=False,
            APP_ENV="production"
        )
        self.assertFalse(verify_recaptcha(None, "login").success)

    def test_missing_production_configuration_fails_closed(self):
        self.app.config.update(
            TESTING=False,
            APP_ENV="production",
            RECAPTCHA_ENABLED=True,
            RECAPTCHA_SECRET_KEY=""
        )
        result = verify_recaptcha("token", "login")
        self.assertEqual(result.reason, "configuration_error")


class RecaptchaRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder="../app/templates")
        self.app.config.update(TESTING=True, SECRET_KEY="test")
        init_csrf(self.app)
        self.app.register_blueprint(auth_bp)
        self.client = self.app.test_client()

    @patch("app.routes.auth.verify_recaptcha")
    @patch("app.routes.auth.get_connection")
    def test_registration_is_blocked_before_database_access(self, connection, verify):
        verify.return_value = Mock(success=False)
        with self.client.session_transaction() as active_session:
            active_session[REGISTRATION_CSRF_SESSION_KEY] = "valid-registration-csrf"
            active_session[REGISTRATION_CSRF_ISSUED_AT_KEY] = int(time.time())
        response = self.client.post(
            "/register-page",
            data={
                "name": "Test User",
                "email": "test@example.com",
                "password": "password",
                "confirm_password": "password",
                "csrf_token": "valid-registration-csrf",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Security verification failed", response.data)
        connection.assert_not_called()

    @patch("app.routes.auth.verify_recaptcha")
    @patch("app.routes.auth.get_connection")
    def test_login_is_blocked_before_database_access(self, connection, verify):
        verify.return_value = Mock(success=False)
        response = self.client.post(
            "/login-page",
            data={"email": "test@example.com", "password": "password"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Security verification failed", response.data)
        connection.assert_not_called()

    @patch("app.routes.auth.verify_recaptcha")
    @patch("app.routes.auth.is_login_locked")
    def test_existing_login_lockout_still_returns_rate_limit(self, locked, verify):
        from datetime import datetime, timedelta

        verify.return_value = Mock(success=True)
        locked.return_value = datetime.utcnow() + timedelta(minutes=1)
        response = self.client.post(
            "/login-page",
            data={"email": "test@example.com", "password": "password"},
        )
        self.assertEqual(response.status_code, 429)
        self.assertIn(b"Too many failed login attempts", response.data)


if __name__ == "__main__":
    unittest.main()
