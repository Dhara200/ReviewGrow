import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from app.routes.auth import auth_bp
from app.services.csrf_service import (
    REGISTRATION_CSRF_ISSUED_AT_KEY,
    init_csrf,
)


class FakeCursor:
    lastrowid = 17

    def execute(self, query, params):
        self.params = params

    def close(self):
        pass


class FakeConnection:
    def __init__(self):
        self.cursor_instance = FakeCursor()

    def cursor(self):
        return self.cursor_instance

    def start_transaction(self):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class RegistrationCsrfTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder="../app/templates")
        self.app.config.update(TESTING=True, SECRET_KEY="registration-csrf-test")
        init_csrf(self.app)
        self.app.register_blueprint(auth_bp)
        self.client = self.app.test_client()

    def token(self, client=None):
        client = client or self.client
        page = client.get("/register-page").get_data(as_text=True)
        match = re.search(r'name="csrf_token" value="([^"]+)"', page)
        self.assertIsNotNone(match)
        return match.group(1)

    def payload(self, token=None):
        data = {
            "name": "Test Owner",
            "email": "owner@example.com",
            "password": "correct-password",
            "confirm_password": "correct-password",
            "recaptcha_token": "provider-token",
        }
        if token is not None:
            data["csrf_token"] = token
        return data

    @patch("app.routes.auth.verify_recaptcha", return_value=SimpleNamespace(success=True))
    @patch("app.routes.auth.create_expired_subscription")
    @patch("app.routes.auth.get_connection", return_value=FakeConnection())
    def test_valid_csrf_and_recaptcha_registers_with_fresh_permanent_session(
        self, connection, subscription, recaptcha
    ):
        token = self.token()
        with self.client.session_transaction() as stale:
            stale["stale_value"] = "remove-me"
            stale["role"] = "admin"
        data = self.payload(token)
        data["name"] = " Test Owner "
        data["email"] = " Owner@Example.COM "
        response = self.client.post("/register-page", data=data)
        self.assertEqual(302, response.status_code)
        self.assertTrue(response.location.endswith("/pricing"))
        connection.assert_called_once()
        subscription.assert_called_once_with(
            17,
            connection=connection.return_value,
            cursor=connection.return_value.cursor_instance,
        )
        recaptcha.assert_called_once()
        inserted = connection.return_value.cursor_instance.params
        self.assertEqual("Test Owner", inserted[0])
        self.assertEqual("owner@example.com", inserted[1])
        with self.client.session_transaction() as registered:
            self.assertTrue(registered.permanent)
            self.assertEqual(17, registered["user_id"])
            self.assertEqual("owner", registered["role"])
            self.assertNotIn("stale_value", registered)

    def assert_rejected_before_route(self, data, client=None):
        client = client or self.client
        with patch("app.routes.auth.verify_recaptcha") as recaptcha, \
             patch("app.routes.auth.get_connection") as connection:
            response = client.post("/register-page", data=data)
        self.assertEqual(403, response.status_code)
        recaptcha.assert_not_called()
        connection.assert_not_called()

    def test_missing_token_is_rejected_before_database(self):
        self.token()
        self.assert_rejected_before_route(self.payload())

    def test_invalid_token_is_rejected_before_database(self):
        self.token()
        self.assert_rejected_before_route(self.payload("invalid"))

    def test_token_from_different_browser_session_is_rejected(self):
        first_token = self.token()
        other = self.app.test_client()
        other.get("/register-page")
        self.assert_rejected_before_route(self.payload(first_token), other)

    def test_expired_token_is_rejected(self):
        token = self.token()
        with self.client.session_transaction() as active:
            active[REGISTRATION_CSRF_ISSUED_AT_KEY] = 1
        self.assert_rejected_before_route(self.payload(token))

    def test_cross_origin_token_is_rejected(self):
        token = self.token()
        with patch("app.routes.auth.get_connection") as connection:
            response = self.client.post(
                "/register-page", data=self.payload(token),
                headers={"Origin": "https://attacker.example"},
            )
        self.assertEqual(403, response.status_code)
        connection.assert_not_called()

    @patch("app.routes.auth.verify_recaptcha", return_value=SimpleNamespace(success=True))
    @patch("app.routes.auth.generate_password_hash")
    @patch("app.routes.auth.get_connection")
    def test_extremely_large_password_is_rejected_before_hashing(
        self, connection, password_hash, recaptcha
    ):
        token = self.token()
        data = self.payload(token)
        data["password"] = "x" * 100_000
        data["confirm_password"] = data["password"]
        response = self.client.post("/register-page", data=data)
        self.assertEqual(400, response.status_code)
        password_hash.assert_not_called()
        connection.assert_not_called()

    @patch("app.routes.auth.verify_recaptcha", return_value=SimpleNamespace(success=True))
    @patch("app.routes.auth.get_connection")
    def test_xss_name_is_rejected_and_reflected_safely(self, connection, recaptcha):
        token = self.token()
        data = self.payload(token)
        data["name"] = '<img src=x onerror=alert(1)>'
        response = self.client.post("/register-page", data=data)
        page = response.get_data(as_text=True)
        self.assertEqual(400, response.status_code)
        self.assertNotIn('<img src=x onerror=alert(1)>', page)
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;', page)
        connection.assert_not_called()

    @patch("app.routes.auth.verify_recaptcha", return_value=SimpleNamespace(success=True))
    @patch("app.routes.auth._create_registered_user", side_effect=RuntimeError("failed"))
    def test_transaction_failure_creates_no_authenticated_session(self, create_user, recaptcha):
        token = self.token()
        response = self.client.post("/register-page", data=self.payload(token))
        self.assertEqual(500, response.status_code)
        with self.client.session_transaction() as active:
            self.assertNotIn("user_id", active)
            self.assertNotIn("role", active)


if __name__ == "__main__":
    unittest.main()
