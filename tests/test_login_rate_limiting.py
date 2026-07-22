import logging
import re
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask
from werkzeug.security import generate_password_hash

from app.routes.auth import auth_bp
from app.config import Config
from app.services.limiter_service import LimitStatus, hash_key
from app.services.login_limiter_service import (
    LoginLimiter,
    LoginLimiterPolicy,
    longest_retry_after,
    validate_login_limiter_config,
)
from app.services.csrf_service import init_csrf


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


class MemoryLimiter:
    """Route-test double only; production has no in-memory fallback."""

    def __init__(self):
        self.rows = {}
        self.calls = []

    def identity(self, scope, key):
        return scope, hash_key(scope, key)

    def check_limit(self, scope, key):
        self.calls.append(("check", scope, key))
        row = self.rows.get(self.identity(scope, key), {"count": 0, "blocked": False, "retry": 0})
        return LimitStatus(row["blocked"], row["count"], row["retry"])

    def record_failure(self, scope, key, *, threshold, window_seconds, block_seconds):
        self.calls.append(("record", scope, key))
        identity = self.identity(scope, key)
        row = self.rows.setdefault(identity, {"count": 0, "blocked": False, "retry": 0})
        row["count"] += 1
        if row["count"] >= threshold:
            row["blocked"] = True
            row["retry"] = block_seconds
        return LimitStatus(row["blocked"], row["count"], row["retry"])

    def reset(self, scope, key):
        self.calls.append(("reset", scope, key))
        return self.rows.pop(self.identity(scope, key), None) is not None


class LoginRateLimitingTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder="../app/templates")
        self.app.config.update(
            TESTING=True, SECRET_KEY="login-limiter-test",
            LOGIN_IP_MAX_ATTEMPTS=6, LOGIN_IP_WINDOW_SECONDS=900,
            LOGIN_IP_BLOCK_SECONDS=600, LOGIN_ACCOUNT_MAX_ATTEMPTS=4,
            LOGIN_ACCOUNT_WINDOW_SECONDS=900, LOGIN_ACCOUNT_BLOCK_SECONDS=800,
            LOGIN_IP_ACCOUNT_MAX_ATTEMPTS=2,
            LOGIN_IP_ACCOUNT_WINDOW_SECONDS=900,
            LOGIN_IP_ACCOUNT_BLOCK_SECONDS=300,
            LOGIN_DUMMY_PASSWORD_HASH=Config.LOGIN_DUMMY_PASSWORD_HASH,
        )
        init_csrf(self.app)
        self.app.register_blueprint(auth_bp)
        self.memory = MemoryLimiter()
        self.limiter = LoginLimiter(
            LoginLimiterPolicy.from_config(self.app.config), self.memory
        )
        self.limiter_patch = patch(
            "app.routes.auth._get_login_limiter", return_value=self.limiter
        )
        self.recaptcha_patch = patch(
            "app.routes.auth.verify_recaptcha",
            return_value=SimpleNamespace(success=True),
        )
        self.limiter_patch.start()
        self.verify_recaptcha = self.recaptcha_patch.start()
        self.addCleanup(self.limiter_patch.stop)
        self.addCleanup(self.recaptcha_patch.stop)
        self.client = self.app.test_client()

    def csrf_token(self):
        page = self.client.get("/login-page").get_data(as_text=True)
        return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)

    def post(self, email="user@example.com", password="wrong", ip="198.51.100.1"):
        return self.client.post(
            "/login-page", data={
                "email": email, "password": password,
                "csrf_token": self.csrf_token(),
            },
            environ_base={"REMOTE_ADDR": ip},
        )

    def unknown_attempt(self, **kwargs):
        with patch("app.routes.auth.get_connection", return_value=FakeConnection()):
            return self.post(**kwargs)

    def test_wrong_password_and_unknown_account_record_all_scopes_normalized(self):
        user = {
            "id": 1, "name": "User", "role": "owner",
            "password_hash": generate_password_hash("correct"),
        }
        with patch("app.routes.auth.get_connection", return_value=FakeConnection(user=user)):
            wrong = self.post(email=" User@Example.COM ")
        self.assertEqual(401, wrong.status_code)
        records = [call for call in self.memory.calls if call[0] == "record"]
        self.assertEqual(["ip", "account", "ip_account"], [call[1] for call in records])
        self.assertEqual("user@example.com", records[1][2])
        self.memory.calls.clear()
        unknown = self.unknown_attempt(email=" Missing@Example.COM ", ip="198.51.100.2")
        self.assertEqual(401, unknown.status_code)
        self.assertEqual(
            ["ip", "account", "ip_account"],
            [call[1] for call in self.memory.calls if call[0] == "record"],
        )

    def test_threshold_failure_is_401_and_next_request_is_429(self):
        first = self.unknown_attempt()
        threshold = self.unknown_attempt()
        blocked = self.unknown_attempt()
        self.assertEqual([401, 401, 429], [first.status_code, threshold.status_code, blocked.status_code])
        self.assertEqual("300", blocked.headers["Retry-After"])
        self.assertIn(b"Too many login attempts", blocked.data)
        self.assertNotIn(b"user@example.com", blocked.data)

    def test_ip_account_ip_and_account_identities_layer_independently(self):
        # Pair threshold: one IP and one account.
        self.unknown_attempt(email="pair@example.com", ip="198.51.100.10")
        self.unknown_attempt(email="pair@example.com", ip="198.51.100.10")
        self.assertEqual(429, self.unknown_attempt(email="pair@example.com", ip="198.51.100.10").status_code)

        # IP threshold: different accounts share one IP.
        for number in range(6):
            self.unknown_attempt(email=f"spray-{number}@example.com", ip="198.51.100.20")
        self.assertEqual(429, self.unknown_attempt(email="new@example.com", ip="198.51.100.20").status_code)

        # Account threshold: different IPs share one normalized account.
        for number in range(4):
            self.unknown_attempt(email="victim@example.com", ip=f"203.0.113.{number + 1}")
        self.assertEqual(429, self.unknown_attempt(email="VICTIM@example.com", ip="203.0.113.99").status_code)

    def test_retry_after_uses_longest_blocked_scope(self):
        account = self.memory.identity("account", "user@example.com")
        pair = self.memory.identity("ip_account", ("198.51.100.1", "user@example.com"))
        self.memory.rows[account] = {"count": 4, "blocked": True, "retry": 733}
        self.memory.rows[pair] = {"count": 2, "blocked": True, "retry": 122}
        response = self.unknown_attempt()
        self.assertEqual(429, response.status_code)
        self.assertEqual("733", response.headers["Retry-After"])
        self.assertTrue(response.headers["Retry-After"].isdigit())

    def test_blocked_ip_precedes_recaptcha_user_lookup_and_password_hash(self):
        identity = self.memory.identity("ip", "198.51.100.1")
        self.memory.rows[identity] = {"count": 6, "blocked": True, "retry": 55}
        with patch("app.routes.auth.get_connection") as connection, patch(
            "app.routes.auth.check_password_hash"
        ) as password_check:
            response = self.post()
        self.assertEqual(429, response.status_code)
        self.verify_recaptcha.assert_not_called()
        connection.assert_not_called()
        password_check.assert_not_called()

    def test_success_resets_account_and_pair_but_not_ip_and_preserves_redirect(self):
        email = "owner@example.com"
        ip = "198.51.100.30"
        for scope, key in (("ip", ip), ("account", email), ("ip_account", (ip, email))):
            self.memory.rows[self.memory.identity(scope, key)] = {
                "count": 1, "blocked": False, "retry": 0,
            }
        user = {
            "id": 7, "name": "Owner", "email": email, "role": "owner",
            "password_hash": generate_password_hash("correct-password"),
        }
        with patch("app.routes.auth.get_connection", return_value=FakeConnection(user=user)), patch(
            "app.routes.auth.has_active_subscription", return_value=False
        ):
            response = self.post(email=" OWNER@example.com ", password="correct-password", ip=ip)
        self.assertEqual(302, response.status_code)
        self.assertEqual("/pricing", response.headers["Location"])
        self.assertIn(self.memory.identity("ip", ip), self.memory.rows)
        self.assertNotIn(self.memory.identity("account", email), self.memory.rows)
        self.assertNotIn(self.memory.identity("ip_account", (ip, email)), self.memory.rows)

    def test_limiter_failure_returns_generic_503_without_sensitive_logs(self):
        failing = MagicMock()
        failing.check_ip.side_effect = RuntimeError(
            "database password=secret email=user@example.com token=sensitive"
        )
        with patch("app.routes.auth._get_login_limiter", return_value=failing), self.assertLogs(
            level=logging.ERROR
        ) as captured:
            response = self.post(password="sensitive-password")
        body = response.get_data(as_text=True)
        logs = " ".join(captured.output)
        self.assertEqual(503, response.status_code)
        self.assertIn("temporarily unavailable", body)
        for secret in ("user@example.com", "sensitive-password", "token=sensitive", "database password"):
            self.assertNotIn(secret, body)
            self.assertNotIn(secret, logs)
        self.assertIn("error_type=RuntimeError", logs)

    def test_unknown_and_existing_accounts_share_throttle_response(self):
        for email in ("known@example.com", "unknown@example.com"):
            self.memory.rows[self.memory.identity("account", email)] = {
                "count": 4, "blocked": True, "retry": 80,
            }
        known = self.unknown_attempt(email="known@example.com")
        unknown = self.unknown_attempt(email="unknown@example.com", ip="198.51.100.2")
        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known.get_data(), unknown.get_data())


class LoginLimiterPolicyTests(unittest.TestCase):
    def test_defaults_keep_account_threshold_above_pair_threshold(self):
        policy = LoginLimiterPolicy.from_config({})
        self.assertEqual((20, 15, 5), (
            policy.ip_threshold, policy.account_threshold, policy.ip_account_threshold
        ))

    def test_longest_retry_and_collision_safe_pair_keys(self):
        self.assertEqual(90, longest_retry_after((
            LimitStatus(True, 1, 30), LimitStatus(True, 1, 90)
        )))
        self.assertNotEqual(
            hash_key("ip_account", ("127.0.0.1", "a-b@example.com")),
            hash_key("ip_account", ("127.0.0.1", "ab@example.com")),
        )

    def test_startup_validation_rejects_bad_values(self):
        app = Flask(__name__)
        defaults = {
            "LOGIN_IP_MAX_ATTEMPTS": 20, "LOGIN_IP_WINDOW_SECONDS": 900,
            "LOGIN_IP_BLOCK_SECONDS": 900, "LOGIN_ACCOUNT_MAX_ATTEMPTS": 15,
            "LOGIN_ACCOUNT_WINDOW_SECONDS": 900, "LOGIN_ACCOUNT_BLOCK_SECONDS": 900,
            "LOGIN_IP_ACCOUNT_MAX_ATTEMPTS": 5,
            "LOGIN_IP_ACCOUNT_WINDOW_SECONDS": 900,
            "LOGIN_IP_ACCOUNT_BLOCK_SECONDS": 900,
        }
        app.config.update(defaults)
        validate_login_limiter_config(app)
        for invalid in (0, True, "5", 1001):
            app.config.update(defaults)
            app.config["LOGIN_IP_MAX_ATTEMPTS"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                validate_login_limiter_config(app)


if __name__ == "__main__":
    unittest.main()
