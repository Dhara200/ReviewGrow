import logging
import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import mysql.connector
from flask import Flask

from app.routes.auth import auth_bp
from app.services.csrf_service import init_csrf


class RegistrationErrorTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder="../app/templates")
        self.app.config.update(TESTING=True, SECRET_KEY="registration-error-test")
        init_csrf(self.app)
        self.app.register_blueprint(auth_bp)
        self.client = self.app.test_client()

    def token(self):
        page = self.client.get("/register-page").get_data(as_text=True)
        return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)

    def payload(self):
        return {
            "csrf_token": self.token(),
            "recaptcha_token": "sensitive-recaptcha-token",
            "name": "Test Owner",
            "email": "owner@example.com",
            "password": "sensitive-password",
            "confirm_password": "sensitive-password",
        }

    def post_with_failure(self, error):
        with patch(
            "app.routes.auth.verify_recaptcha",
            return_value=SimpleNamespace(success=True),
        ), patch("app.routes.auth._create_registered_user", side_effect=error):
            return self.client.post("/register-page", data=self.payload())

    def test_duplicate_email_returns_safe_deterministic_message(self):
        error = mysql.connector.IntegrityError(
            msg="Duplicate entry owner@example.com for key users.email", errno=1062
        )
        response = self.post_with_failure(error)
        page = response.get_data(as_text=True)
        self.assertEqual(409, response.status_code)
        self.assertIn("An account with this email already exists.", page)
        self.assertNotIn("Duplicate entry", page)
        self.assertNotIn("users.email", page)
        self.assertNotIn("Traceback", page)
        with self.client.session_transaction() as active:
            self.assertNotIn("user_id", active)

    def test_nonduplicate_database_failure_returns_generic_message(self):
        error = mysql.connector.DataError(
            msg="Data too long for column users.name", errno=1406
        )
        response = self.post_with_failure(error)
        page = response.get_data(as_text=True)
        self.assertEqual(500, response.status_code)
        self.assertIn("We could not create your account right now. Please try again.", page)
        self.assertNotIn("users.name", page)
        self.assertNotIn("Data too long", page)

    def test_subscription_and_connection_failures_are_generic(self):
        for error in (
            RuntimeError("subscription insert failed password=sensitive-password"),
            mysql.connector.InterfaceError("database connection unavailable"),
        ):
            with self.subTest(error=type(error).__name__):
                response = self.post_with_failure(error)
                self.assertEqual(500, response.status_code)
                self.assertIn(
                    "We could not create your account right now. Please try again.",
                    response.get_data(as_text=True),
                )
                self.assertNotIn("Traceback", response.get_data(as_text=True))

    def test_logs_exclude_password_token_and_exception_message(self):
        error = RuntimeError(
            "password=sensitive-password token=sensitive-recaptcha-token SECRET_KEY=secret"
        )
        with self.assertLogs(level=logging.ERROR) as captured:
            response = self.post_with_failure(error)
        self.assertEqual(500, response.status_code)
        logs = " ".join(captured.output)
        self.assertIn("error_type=RuntimeError", logs)
        self.assertNotIn("sensitive-password", logs)
        self.assertNotIn("sensitive-recaptcha-token", logs)
        self.assertNotIn("SECRET_KEY", logs)


if __name__ == "__main__":
    unittest.main()
