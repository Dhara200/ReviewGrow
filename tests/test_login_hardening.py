import re
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask
from werkzeug.security import generate_password_hash

from app.config import Config
from app.routes.auth import auth_bp
from app.services.csrf_service import LOGIN_CSRF_ISSUED_AT_KEY, init_csrf
from app.services.limiter_service import LimitStatus
from app.services.login_security_service import validate_login_dummy_hash


class TrackingCursor:
    def __init__(self, user=None, error=None):
        self.user = user
        self.error = error
        self.closed = False

    def execute(self, query, params):
        if self.error:
            raise self.error

    def fetchone(self):
        return self.user

    def close(self):
        self.closed = True


class TrackingConnection:
    def __init__(self, user=None, error=None):
        self.cursor_instance = TrackingCursor(user, error)
        self.closed = False
        self.rolled_back = False

    def cursor(self, dictionary=False):
        return self.cursor_instance

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class LoginHardeningTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder="../app/templates")
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="login-hardening-secret",
            LOGIN_DUMMY_PASSWORD_HASH=Config.LOGIN_DUMMY_PASSWORD_HASH,
        )
        init_csrf(self.app)
        self.app.register_blueprint(auth_bp)
        self.client = self.app.test_client()
        self.limiter = MagicMock()
        self.limiter.check_ip.return_value = LimitStatus(False, 0, 0)
        self.limiter.check_account_and_pair.return_value = (
            LimitStatus(False, 0, 0), LimitStatus(False, 0, 0)
        )
        self.limiter_patch = patch(
            "app.routes.auth._get_login_limiter", return_value=self.limiter
        )
        self.recaptcha_patch = patch(
            "app.routes.auth.verify_recaptcha",
            return_value=SimpleNamespace(success=True),
        )
        self.limiter_patch.start()
        self.recaptcha = self.recaptcha_patch.start()
        self.addCleanup(self.limiter_patch.stop)
        self.addCleanup(self.recaptcha_patch.stop)

    def token(self, client=None):
        client = client or self.client
        page = client.get("/login-page").get_data(as_text=True)
        match = re.search(r'name="csrf_token" value="([^"]+)"', page)
        self.assertIsNotNone(match)
        return match.group(1)

    def payload(self, token=None, email="user@example.com", password="wrong"):
        data = {"email": email, "password": password, "recaptcha_token": "provider-token"}
        if token is not None:
            data["csrf_token"] = token
        return data

    def post(self, **changes):
        token = changes.pop("token", self.token())
        return self.client.post("/login-page", data=self.payload(token, **changes))

    def assert_not_authenticated(self):
        with self.client.session_transaction() as active:
            self.assertNotIn("user_id", active)
            self.assertNotIn("role", active)

    def test_form_has_session_bound_token_and_browser_bounds(self):
        page = self.client.get("/login-page").get_data(as_text=True)
        self.assertRegex(page, r'name="csrf_token" value="[^"]{40,}"')
        self.assertRegex(page, r'name="email"[^>]*autocomplete="email"[^>]*maxlength="254"[^>]*required')
        self.assertRegex(page, r'name="password"[^>]*autocomplete="current-password"[^>]*maxlength="128"[^>]*required')

    def test_missing_invalid_query_other_session_and_expired_csrf_are_rejected(self):
        valid = self.token()
        other = self.app.test_client()
        other_token = self.token(other)
        cases = (
            ({}, None),
            ({"csrf_token": "invalid"}, None),
            ({"csrf_token": other_token}, None),
            ({}, valid),  # query parameter must not be accepted
        )
        for form_extra, query_token in cases:
            data = self.payload()
            data.update(form_extra)
            url = "/login-page" + (f"?csrf_token={query_token}" if query_token else "")
            with self.subTest(form_extra=form_extra, query=query_token), patch(
                "app.routes.auth.check_password_hash"
            ) as password_check, patch("app.routes.auth.get_connection") as connection:
                response = self.client.post(url, data=data)
                self.assertEqual(403, response.status_code)
                password_check.assert_not_called()
                connection.assert_not_called()
        with self.client.session_transaction() as active:
            active[LOGIN_CSRF_ISSUED_AT_KEY] = int(time.time()) - 3601
        response = self.client.post("/login-page", data=self.payload(valid))
        self.assertEqual(403, response.status_code)
        self.assert_not_authenticated()
        self.limiter.record_failure.assert_not_called()

    def test_valid_csrf_allows_processing_and_success_invalidates_old_token(self):
        token = self.token()
        user = {
            "id": 7, "name": "Owner", "role": "owner",
            "password_hash": generate_password_hash("correct password"),
        }
        connection = TrackingConnection(user)
        with patch("app.routes.auth.get_connection", return_value=connection), patch(
            "app.routes.auth.has_active_subscription", return_value=False
        ):
            response = self.client.post(
                "/login-page", data=self.payload(
                    token, email=" User@Example.COM ", password="correct password"
                )
            )
        self.assertEqual(302, response.status_code)
        self.assertTrue(connection.closed)
        self.assertTrue(connection.cursor_instance.closed)
        with self.client.session_transaction() as authenticated:
            self.assertEqual(7, authenticated["user_id"])
        # Logout-like clearing represents the next pre-authentication session; the
        # prior token was removed by successful session rotation.
        with self.client.session_transaction() as active:
            active.clear()
        reused = self.client.post("/login-page", data=self.payload(token))
        self.assertEqual(403, reused.status_code)

    def test_input_bounds_and_controls_reject_before_lookup_or_hashing(self):
        cases = (
            {"email": ""}, {"email": "x" * 255 + "@example.com"},
            {"email": "user\x00@example.com"}, {"email": "user\r\n@example.com"},
            {"password": ""}, {"password": "x" * 129},
            {"password": "bad\x00password"}, {"password": "bad\npassword"},
        )
        for changes in cases:
            with self.subTest(changes=changes), patch(
                "app.routes.auth.get_connection"
            ) as connection, patch("app.routes.auth.check_password_hash") as password_check:
                response = self.post(**changes)
                self.assertEqual(401, response.status_code)
                connection.assert_not_called()
                password_check.assert_not_called()

    def test_password_is_not_trimmed_and_unicode_and_spaces_are_supported(self):
        password = "  päss word  "
        user = {
            "id": 7, "name": "Owner", "role": "owner",
            "password_hash": generate_password_hash(password),
        }
        with patch("app.routes.auth.get_connection", return_value=TrackingConnection(user)), patch(
            "app.routes.auth.has_active_subscription", return_value=False
        ):
            self.assertEqual(302, self.post(password=password).status_code)

    def test_unknown_account_uses_stable_dummy_and_records_all_scopes(self):
        with patch("app.routes.auth.get_connection", return_value=TrackingConnection()), patch(
            "app.routes.auth.check_password_hash", return_value=False
        ) as password_check:
            first = self.post(email=" Missing@Example.COM ", password="secret")
            second = self.post(email="missing@example.com", password="secret")
        self.assertEqual((401, 401), (first.status_code, second.status_code))
        self.assertEqual(2, password_check.call_count)
        for call in password_check.call_args_list:
            self.assertEqual(Config.LOGIN_DUMMY_PASSWORD_HASH, call.args[0])
        self.assertEqual(
            [("missing@example.com", "127.0.0.1")] * 2,
            [call.args for call in self.limiter.record_failure.call_args_list],
        )

    def test_existing_account_uses_stored_hash_and_invalid_responses_match(self):
        stored_hash = generate_password_hash("correct")
        user = {"id": 7, "name": "Owner", "role": "owner", "password_hash": stored_hash}
        with patch("app.routes.auth.get_connection", side_effect=[TrackingConnection(), TrackingConnection(user)]):
            unknown = self.post(email="same@example.com")
            existing = self.post(email="same@example.com")
        self.assertEqual(401, unknown.status_code)
        self.assertEqual(unknown.get_data(), existing.get_data())

    def test_database_failure_rolls_back_closes_and_returns_generic_503(self):
        error = RuntimeError("SELECT password_hash FROM users secret@example.com")
        connection = TrackingConnection(error=error)
        with patch("app.routes.auth.get_connection", return_value=connection):
            response = self.post(password="sensitive-password")
        body = response.get_data(as_text=True)
        self.assertEqual(503, response.status_code)
        self.assertIn("temporarily unavailable", body)
        self.assertNotIn("SELECT", body)
        self.assertNotIn("secret@example.com", body)
        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.cursor_instance.closed)
        self.assertTrue(connection.closed)
        self.assert_not_authenticated()

    def test_malformed_hash_and_unexpected_failure_return_deterministic_500(self):
        user = {"id": 7, "name": "Owner", "role": "owner", "password_hash": "malformed"}
        with patch("app.routes.auth.get_connection", return_value=TrackingConnection(user)), patch(
            "app.routes.auth.check_password_hash",
            side_effect=ValueError("malformed hash table=users"),
        ):
            first = self.post(password="sensitive")
            second = self.post(password="sensitive")
        self.assertEqual((500, 500), (first.status_code, second.status_code))
        self.assertEqual(first.get_data(), second.get_data())
        self.assertNotIn(b"malformed", first.data)
        self.assertNotIn(b"users", first.data)
        self.assert_not_authenticated()

    def test_post_auth_database_failure_cleans_partial_session_and_resources(self):
        user = {
            "id": 7, "name": "Owner", "role": "owner",
            "password_hash": generate_password_hash("correct"),
        }
        user_connection = TrackingConnection(user)
        business_connection = TrackingConnection(
            error=RuntimeError("business database unavailable")
        )
        with patch(
            "app.routes.auth.get_connection",
            side_effect=[user_connection, business_connection],
        ), patch("app.routes.auth.has_active_subscription", return_value=True):
            response = self.post(password="correct")
        self.assertEqual(503, response.status_code)
        self.assertTrue(business_connection.rolled_back)
        self.assertTrue(business_connection.cursor_instance.closed)
        self.assertTrue(business_connection.closed)
        self.assert_not_authenticated()

    def test_dummy_hash_startup_validation(self):
        validate_login_dummy_hash(self.app)
        for value in (None, "", "plaintext", "scrypt:bad"):
            self.app.config["LOGIN_DUMMY_PASSWORD_HASH"] = value
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                validate_login_dummy_hash(self.app)


if __name__ == "__main__":
    unittest.main()
