import re
import inspect
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask
from werkzeug.security import generate_password_hash

from app.routes.auth import auth_bp
from app.services.csrf_service import init_csrf
from app.services.limiter_service import LimitStatus
from app.services.login_otp_service import (
    CreatedChallenge, OtpCooldownError, OtpExpiredError, OtpInvalidError,
    OtpLockedError, OtpRateLimitError,
    create_login_otp_challenge, generate_otp, verify_otp_hash, _stored_hash,
)


class LoginOtpTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder="../app/templates")
        self.app.config.update(
            TESTING=True, SECRET_KEY="otp-test-secret-key-long-enough",
            LOGIN_OTP_ENABLED=True, LOGIN_OTP_EXPIRY_MINUTES=5,
            LOGIN_OTP_PENDING_SESSION_MINUTES=10,
            LOGIN_OTP_RESEND_COOLDOWN_SECONDS=60,
            LOGIN_DUMMY_PASSWORD_HASH=generate_password_hash("dummy-password"),
        )
        init_csrf(self.app)
        self.app.register_blueprint(auth_bp)
        self.client = self.app.test_client()
        self.user = {"id": 7, "name": "Owner", "email": "owner@example.com",
                     "role": "owner", "password_hash": generate_password_hash("correct")}
        self.limiter = MagicMock()
        self.limiter.check_ip.return_value = LimitStatus(False, 0, 0)
        self.limiter.check_account_and_pair.return_value = (
            LimitStatus(False, 0, 0), LimitStatus(False, 0, 0)
        )

    def csrf(self, path="/login-page"):
        page = self.client.get(path).get_data(as_text=True)
        return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)

    def login(self, password="correct"):
        token = self.csrf()
        with patch("app.routes.auth._get_login_limiter", return_value=self.limiter), \
             patch("app.routes.auth.verify_recaptcha", return_value=SimpleNamespace(success=True)), \
             patch("app.routes.auth._find_login_user", return_value=self.user), \
             patch("app.routes.auth.create_login_otp_challenge",
                   return_value=CreatedChallenge(91, "o***@example.com")) as create:
            response = self.client.post("/login-page", data={
                "csrf_token": token, "recaptcha_token": "token",
                "email": "owner@example.com", "password": password,
            })
        return response, create

    def set_pending(self):
        with self.client.session_transaction() as active:
            active["pending_login_user_id"] = 7
            active["pending_login_challenge_id"] = 91
            active["pending_login_started_at"] = int(time.time())
            active["pending_login_masked_email"] = "o***@example.com"

    def test_secure_generator_is_six_digits_and_supports_leading_zeroes(self):
        with patch("app.services.login_otp_service.secrets.randbelow", return_value=7) as secure:
            self.assertEqual("000007", generate_otp())
        secure.assert_called_once_with(1_000_000)

    def test_hmac_verification_accepts_correct_and_rejects_incorrect(self):
        with patch("app.services.login_otp_service.Config.SECRET_KEY",
                   "otp-test-secret-key-long-enough"):
            stored = _stored_hash(12, "012345")
            self.assertNotIn("012345", stored)
            self.assertTrue(verify_otp_hash(12, stored, "012345"))
            self.assertFalse(verify_otp_hash(12, stored, "999999"))

    def test_challenge_storage_contains_hash_only_and_queues_high_priority(self):
        class Cursor:
            lastrowid = 44
            rowcount = 1
            def __init__(self): self.executions = []
            def execute(self, sql, params): self.executions.append((sql, params))
            def fetchall(self): return []
            def fetchone(self): return {"elapsed": 61}
            def close(self): pass
        class Connection:
            def __init__(self): self.cursor_value = Cursor()
            def cursor(self, dictionary=False): return self.cursor_value
            def start_transaction(self): pass
            def commit(self): pass
            def rollback(self): pass
            def close(self): pass
        connection = Connection()
        with patch("app.services.login_otp_service.get_connection", return_value=connection), \
             patch("app.services.login_otp_service.generate_otp", return_value="012345"), \
             patch("app.services.login_otp_service.enqueue_email") as enqueue, \
             patch("app.services.login_otp_service.Config.SECRET_KEY",
                   "otp-test-secret-key-long-enough"):
            result = create_login_otp_challenge(
                self.user, "127.0.0.1", "test-agent"
            )
        self.assertEqual(44, result.id)
        self.assertTrue(any("invalidated_at=UTC_TIMESTAMP" in sql
                            for sql, _params in connection.cursor_value.executions))
        all_params = [value for _sql, params in connection.cursor_value.executions
                      for value in params]
        self.assertNotIn("012345", all_params)
        self.assertEqual(10, enqueue.call_args.kwargs["priority"])
        self.assertEqual("login_otp:44", enqueue.call_args.kwargs["deduplication_key"])

    def test_password_success_creates_only_pending_session(self):
        response, create = self.login()
        self.assertEqual(302, response.status_code)
        self.assertTrue(response.location.endswith("/login/verify-otp"))
        create.assert_called_once()
        with self.client.session_transaction() as active:
            self.assertNotIn("user_id", active)
            self.assertEqual(7, active["pending_login_user_id"])
            self.assertEqual(91, active["pending_login_challenge_id"])

    def test_incorrect_password_never_creates_challenge(self):
        response, create = self.login(password="incorrect")
        self.assertEqual(401, response.status_code)
        create.assert_not_called()

    def test_queue_or_challenge_failure_never_authenticates(self):
        token = self.csrf()
        with patch("app.routes.auth._get_login_limiter", return_value=self.limiter), \
             patch("app.routes.auth.verify_recaptcha", return_value=SimpleNamespace(success=True)), \
             patch("app.routes.auth._find_login_user", return_value=self.user), \
             patch("app.routes.auth.create_login_otp_challenge", side_effect=RuntimeError("queue")):
            response = self.client.post("/login-page", data={
                "csrf_token": token, "recaptcha_token": "token",
                "email": "owner@example.com", "password": "correct"})
        self.assertEqual(503, response.status_code)
        with self.client.session_transaction() as active:
            self.assertNotIn("user_id", active)

    def test_disabled_feature_preserves_direct_login(self):
        self.app.config["LOGIN_OTP_ENABLED"] = False
        token = self.csrf()
        with patch("app.routes.auth._get_login_limiter", return_value=self.limiter), \
             patch("app.routes.auth.verify_recaptcha", return_value=SimpleNamespace(success=True)), \
             patch("app.routes.auth._find_login_user", return_value=self.user), \
             patch("app.routes.auth.has_active_subscription", return_value=False), \
             patch("app.routes.auth.create_login_otp_challenge") as create:
            response = self.client.post("/login-page", data={
                "csrf_token": token, "recaptcha_token": "token",
                "email": "owner@example.com", "password": "correct"})
        self.assertTrue(response.location.endswith("/pricing"))
        create.assert_not_called()
        with self.client.session_transaction() as active:
            self.assertEqual(7, active["user_id"])

    def test_correct_otp_authenticates_clears_pending_and_cannot_replay(self):
        self.set_pending()
        token = self.csrf("/login/verify-otp")
        verified = {k: self.user[k] for k in ("id", "name", "email", "role")}
        with patch("app.routes.auth.verify_login_otp", return_value=verified) as verify, \
             patch("app.routes.auth.has_active_subscription", return_value=False):
            first = self.client.post("/login/verify-otp", data={
                "csrf_token": token, "otp_code": "012345"})
            second = self.client.post("/login/verify-otp", data={
                "csrf_token": token, "otp_code": "012345"})
        self.assertTrue(first.location.endswith("/pricing"))
        self.assertIn(second.status_code, (302, 403))
        verify.assert_called_once_with(7, 91, "012345")
        with self.client.session_transaction() as active:
            self.assertEqual(7, active["user_id"])
            self.assertNotIn("pending_login_user_id", active)

    def test_invalid_expired_and_locked_outcomes_are_safe(self):
        for error, expected_status in ((OtpInvalidError(), 400), (OtpExpiredError(), 400),
                                       (OtpLockedError(), 302)):
            with self.subTest(error=type(error).__name__):
                self.client.get("/logout")
                self.set_pending()
                token = self.csrf("/login/verify-otp")
                with patch("app.routes.auth.verify_login_otp", side_effect=error):
                    response = self.client.post("/login/verify-otp", data={
                        "csrf_token": token, "otp_code": "123456"})
                self.assertEqual(expected_status, response.status_code)
                with self.client.session_transaction() as active:
                    self.assertNotIn("user_id", active)

    def test_otp_page_is_no_cache_and_resend_requires_csrf(self):
        self.set_pending()
        page = self.client.get("/login/verify-otp")
        self.assertIn("no-store", page.headers["Cache-Control"])
        self.assertNotIn("owner@example.com", page.get_data(as_text=True))
        response = self.client.post("/login/resend-otp", data={})
        self.assertEqual(403, response.status_code)

    def test_resend_replaces_challenge_id(self):
        self.set_pending()
        token = self.csrf("/login/verify-otp")
        user = {k: self.user[k] for k in ("id", "name", "email", "role")}
        with patch("app.routes.auth._find_login_user_by_id", return_value=user), \
             patch("app.routes.auth.create_login_otp_challenge",
                   return_value=CreatedChallenge(92, "o***@example.com")):
            response = self.client.post("/login/resend-otp", data={"csrf_token": token})
        self.assertEqual(200, response.status_code)
        with self.client.session_transaction() as active:
            self.assertEqual(92, active["pending_login_challenge_id"])

    def test_resend_cooldown_and_rate_limit_return_429(self):
        for error in (OtpCooldownError(), OtpRateLimitError()):
            with self.subTest(error=type(error).__name__):
                self.set_pending()
                token = self.csrf("/login/verify-otp")
                with patch("app.routes.auth._find_login_user_by_id", return_value=self.user), \
                     patch("app.routes.auth.create_login_otp_challenge", side_effect=error):
                    response = self.client.post(
                        "/login/resend-otp", data={"csrf_token": token}
                    )
                self.assertEqual(429, response.status_code)

    def test_missing_pending_state_redirects_and_logout_clears_pending(self):
        self.assertTrue(
            self.client.get("/login/verify-otp").location.endswith("/login-page")
        )
        self.set_pending()
        response = self.client.post("/logout")
        self.assertTrue(response.location.endswith("/login-page"))
        with self.client.session_transaction() as active:
            self.assertNotIn("pending_login_user_id", active)

    def test_verification_uses_row_lock_and_single_use_update_guard(self):
        from app.services import login_otp_service
        source = inspect.getsource(login_otp_service.verify_login_otp)
        self.assertIn("FOR UPDATE", source)
        self.assertIn("used_at IS NULL AND invalidated_at IS NULL", source)
        self.assertIn("cursor.rowcount != 1", source)


if __name__ == "__main__":
    unittest.main()
