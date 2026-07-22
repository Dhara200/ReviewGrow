import json
import logging
import re
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask
from werkzeug.security import generate_password_hash

from app.config import Config
from app.routes.auth import auth_bp
from app.services.csrf_service import init_csrf
from app.services.limiter_service import LimitStatus
from app.services.security_audit_service import (
    SecurityAuditService,
    validate_security_audit_config,
)


TEST_KEY = "deterministic-security-audit-hmac-key-0123456789abcdef"


class SecurityAuditServiceTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger(f"security-audit-test-{id(self)}")
        self.logger.propagate = False
        self.service = SecurityAuditService(
            self.logger, enabled=True, hmac_key=TEST_KEY
        )

    def capture(self, event_name="login_invalid_credentials", **kwargs):
        with self.assertLogs(self.logger, level=logging.INFO) as captured:
            event = self.service.emit(event_name, **kwargs)
        rendered = " ".join(captured.output)
        payload = json.loads(rendered.split(SecurityAuditService.PREFIX, 1)[1])
        return event, payload, rendered

    def test_complete_output_contains_only_pseudonymous_identities(self):
        secrets = {
            "email": " User@Example.COM ",
            "client_ip": "198.51.100.25",
        }
        _, payload, rendered = self.capture(
            email=secrets["email"], client_ip=secrets["client_ip"], http_status=401
        )
        self.assertEqual(64, len(payload["account_key"]))
        self.assertEqual(64, len(payload["client_ip_key"]))
        for forbidden in (
            "User@Example.COM", "user@example.com", "198.51.100.25",
            "sensitive-password", "password-hash", "csrf-token",
            "recaptcha-token", "session-cookie", TEST_KEY,
        ):
            self.assertNotIn(forbidden, rendered)

    def test_account_and_client_pseudonyms_are_stable_and_distinct(self):
        self.assertEqual(
            self.service.account_key(" User@Example.COM "),
            self.service.account_key("user@example.com"),
        )
        self.assertNotEqual(
            self.service.account_key("one@example.com"),
            self.service.account_key("two@example.com"),
        )
        self.assertEqual(
            self.service.client_key("2001:0db8::1"),
            self.service.client_key("2001:db8:0:0:0:0:0:1"),
        )
        self.assertNotEqual(
            self.service.client_key("198.51.100.1"),
            self.service.client_key("198.51.100.2"),
        )

    def test_missing_and_invalid_identities_use_placeholders(self):
        self.assertEqual("missing", self.service.account_key(""))
        self.assertEqual("invalid", self.service.account_key("bad\nemail", valid=False))
        self.assertEqual("unknown", self.service.client_key("not-an-ip"))
        self.assertEqual("unknown", self.service.client_key(None))

    def test_unknown_and_prohibited_fields_are_rejected(self):
        for field in (
            "password", "password_hash", "dummy_hash", "csrf_token",
            "recaptcha_token", "session_id", "raw_ip", "sql", "unknown_field",
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.service.emit("login_internal_error", **{field: "sensitive"})

    def test_disabled_service_emits_nothing(self):
        disabled = SecurityAuditService(self.logger, enabled=False, hmac_key="")
        with patch.object(self.logger, "info") as info:
            self.assertIsNone(disabled.emit("login_success", email="a@b.com"))
        info.assert_not_called()

    def test_production_configuration_requires_strong_key_without_exposing_it(self):
        app = Flask(__name__)
        for key in (
            "", "short", "a" * 64,
            "replace_with_a_unique_random_security_audit_key",
        ):
            app.config.update(SECURITY_AUDIT_ENABLED=True, SECURITY_AUDIT_HMAC_KEY=key)
            with self.subTest(key_length=len(key)), self.assertRaises(RuntimeError) as raised:
                validate_security_audit_config(app)
            if key:
                self.assertNotIn(key, str(raised.exception))
        app.config.update(
            SECURITY_AUDIT_ENABLED=True, SECURITY_AUDIT_HMAC_KEY=TEST_KEY
        )
        validate_security_audit_config(app)

    def test_schema_is_small_stable_and_severity_is_mapped(self):
        cases = (
            ("login_success", "INFO", "success", 302),
            ("login_rate_limited", "WARNING", "blocked", 429),
            ("login_internal_error", "ERROR", "error", 500),
        )
        for event_name, level, outcome, status in cases:
            with self.subTest(event_name=event_name):
                kwargs = {"http_status": status}
                if event_name == "login_rate_limited":
                    kwargs.update(retry_after_seconds=30, limiter_scope="ip")
                _, payload, rendered = self.capture(event_name, **kwargs)
                self.assertIn(f"{level}:", rendered)
                self.assertEqual(outcome, payload["outcome"])
                self.assertEqual(1, payload["event_version"])
                self.assertRegex(payload["timestamp_utc"], r"Z$")
                self.assertLessEqual(len(payload), 9)


class FakeCursor:
    def __init__(self, user=None, error=None):
        self.user = user
        self.error = error

    def execute(self, query, params):
        if self.error:
            raise self.error

    def fetchone(self):
        return self.user

    def close(self):
        pass


class FakeConnection:
    def __init__(self, user=None, error=None):
        self.cursor_instance = FakeCursor(user, error)

    def cursor(self, dictionary=False):
        return self.cursor_instance

    def rollback(self):
        pass

    def close(self):
        pass


class SecurityAuditRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder="../app/templates")
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="audit-route-test",
            LOGIN_DUMMY_PASSWORD_HASH=Config.LOGIN_DUMMY_PASSWORD_HASH,
            SECURITY_AUDIT_ENABLED=True,
            SECURITY_AUDIT_HMAC_KEY=TEST_KEY,
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
            return_value=SimpleNamespace(success=True, reason=""),
        )
        self.limiter_patch.start()
        self.recaptcha = self.recaptcha_patch.start()
        self.addCleanup(self.limiter_patch.stop)
        self.addCleanup(self.recaptcha_patch.stop)

    def token(self):
        page = self.client.get("/login-page").get_data(as_text=True)
        return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)

    def data(self, **changes):
        payload = {
            "csrf_token": self.token(), "recaptcha_token": "sensitive-recaptcha-token",
            "email": " User@Example.COM ", "password": "sensitive-password",
        }
        payload.update(changes)
        return payload

    def post_and_events(self, *, data=None, connection=None):
        context = patch(
            "app.routes.auth.get_connection",
            return_value=connection if connection is not None else FakeConnection(),
        )
        with context, self.assertLogs(self.app.logger, level=logging.INFO) as captured:
            response = self.client.post("/login-page", data=data or self.data())
        rendered = " ".join(captured.output)
        lines = [line for line in captured.output if SecurityAuditService.PREFIX in line]
        return response, lines, rendered

    def assert_one_event(self, lines, event_name):
        self.assertEqual(1, len(lines), lines)
        self.assertIn(f'"event_name":"{event_name}"', lines[0])

    def test_invalid_unknown_and_existing_accounts_have_same_safe_event(self):
        unknown, unknown_lines, unknown_log = self.post_and_events()
        user = {
            "id": 7, "name": "Owner", "role": "owner",
            "password_hash": generate_password_hash("different-password"),
        }
        existing, existing_lines, existing_log = self.post_and_events(
            connection=FakeConnection(user)
        )
        self.assertEqual((401, 401), (unknown.status_code, existing.status_code))
        self.assert_one_event(unknown_lines, "login_invalid_credentials")
        self.assert_one_event(existing_lines, "login_invalid_credentials")
        unknown_payload = json.loads(unknown_lines[0].split(SecurityAuditService.PREFIX, 1)[1])
        existing_payload = json.loads(existing_lines[0].split(SecurityAuditService.PREFIX, 1)[1])
        self.assertEqual(set(unknown_payload), set(existing_payload))
        for rendered in (unknown_log, existing_log):
            for secret in (
                "User@Example.COM", "user@example.com", "sensitive-password",
                "sensitive-recaptcha-token", "127.0.0.1", Config.LOGIN_DUMMY_PASSWORD_HASH,
            ):
                self.assertNotIn(secret, rendered)

    def test_rate_limit_csrf_recaptcha_and_input_each_emit_one_event(self):
        self.limiter.check_ip.return_value = LimitStatus(True, 20, 77)
        response, lines, _ = self.post_and_events()
        self.assertEqual(429, response.status_code)
        self.assertEqual("77", response.headers["Retry-After"])
        self.assert_one_event(lines, "login_rate_limited")

        self.limiter.check_ip.return_value = LimitStatus(False, 0, 0)
        response, lines, _ = self.post_and_events(data={
            "email": "user@example.com", "password": "wrong", "csrf_token": "bad"
        })
        self.assertEqual(403, response.status_code)
        self.assert_one_event(lines, "login_csrf_rejected")

        self.recaptcha.return_value = SimpleNamespace(success=False, reason="low_score")
        response, lines, _ = self.post_and_events()
        self.assertEqual(400, response.status_code)
        self.assert_one_event(lines, "login_recaptcha_rejected")

        self.recaptcha.return_value = SimpleNamespace(success=True, reason="")
        response, lines, _ = self.post_and_events(data=self.data(password="x" * 129))
        self.assertEqual(401, response.status_code)
        self.assert_one_event(lines, "login_input_rejected")

    def test_backend_failure_events_are_single_errors(self):
        self.limiter.check_ip.side_effect = RuntimeError("limiter SQL secret")
        response, lines, rendered = self.post_and_events()
        self.assertEqual(503, response.status_code)
        self.assert_one_event(lines, "login_limiter_unavailable")
        self.assertNotIn("limiter SQL secret", rendered)

        self.limiter.check_ip.side_effect = None
        response, lines, rendered = self.post_and_events(
            connection=FakeConnection(error=RuntimeError("users table secret"))
        )
        self.assertEqual(503, response.status_code)
        self.assert_one_event(lines, "login_backend_unavailable")
        self.assertNotIn("users table secret", rendered)

        with patch(
            "app.routes.auth.check_password_hash", side_effect=ValueError("hash secret")
        ):
            response, lines, rendered = self.post_and_events()
        self.assertEqual(500, response.status_code)
        self.assert_one_event(lines, "login_internal_error")
        self.assertNotIn("hash secret", rendered)

    def test_success_emits_one_event_without_role_or_session(self):
        user = {
            "id": 7, "name": "Owner", "role": "owner",
            "password_hash": generate_password_hash("correct-password"),
        }
        with patch("app.routes.auth.has_active_subscription", return_value=False):
            response, lines, rendered = self.post_and_events(
                data=self.data(password="correct-password"),
                connection=FakeConnection(user),
            )
        self.assertEqual(302, response.status_code)
        self.assert_one_event(lines, "login_success")
        self.assertNotIn("owner", rendered.lower())
        self.assertNotIn("session", rendered.lower())


if __name__ == "__main__":
    unittest.main()
