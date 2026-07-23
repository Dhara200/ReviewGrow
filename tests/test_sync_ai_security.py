import logging
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask

from app.routes.analysis import analysis_bp
from app.services.ai_service import AIResult
from app.services.csrf_service import init_csrf
from app.services.limiter_service import LimitStatus
from app.services import sync_ai_security_service as security


class CountingLimiter:
    def __init__(self, fail=False):
        self.counts = {}
        self.fail = fail

    def record_failure(
        self, scope, key, *, threshold, window_seconds, block_seconds
    ):
        if self.fail:
            raise RuntimeError("database password=secret")
        identity = (scope, str(key))
        self.counts[identity] = self.counts.get(identity, 0) + 1
        count = self.counts[identity]
        return LimitStatus(count >= threshold, count, 600 if count >= threshold else 0)


class SyncAISecurityServiceTests(unittest.TestCase):
    def test_review_text_validation_bounds_unicode_and_controls(self):
        self.assertEqual("hello", security.validate_review_text("  hello  "))
        self.assertEqual("é नमस्ते", security.validate_review_text("é नमस्ते\n"))
        self.assertEqual("x" * 5000, security.validate_review_text("x" * 5000))
        for invalid in (None, "", " \r\n\t ", "x" * 5001, "safe\x00unsafe"):
            with self.subTest(invalid=repr(invalid)[:30]):
                with self.assertRaises(ValueError):
                    security.validate_review_text(invalid)

    def test_user_limit_allows_ten_and_blocks_eleventh_across_actions(self):
        limiter = CountingLimiter()
        for _ in range(10):
            scope, status = security.consume_ai_rate_limits(
                7, "198.51.100.1", limiter
            )
            self.assertIsNone(scope)
            self.assertIsNone(status)
        scope, status = security.consume_ai_rate_limits(
            7, "198.51.100.2", limiter
        )
        self.assertEqual("user", scope)
        self.assertTrue(status.blocked)

    def test_ip_limit_is_shared_across_accounts(self):
        limiter = CountingLimiter()
        for user_id in range(1, 21):
            scope, status = security.consume_ai_rate_limits(
                user_id, "198.51.100.9", limiter
            )
            self.assertIsNone(scope)
            self.assertIsNone(status)
        scope, status = security.consume_ai_rate_limits(
            99, "198.51.100.9", limiter
        )
        self.assertEqual("ip", scope)
        self.assertTrue(status.blocked)

    def test_limiter_failure_is_sanitized_and_fails_closed(self):
        with self.assertRaises(security.AISecurityUnavailable) as raised:
            security.consume_ai_rate_limits(
                7, "198.51.100.1", CountingLimiter(fail=True)
            )
        self.assertNotIn("password", str(raised.exception))

    def test_quota_slot_uses_database_lock_and_current_calendar_month(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            {"ACQUIRED": 1},
            {"SUCCESSFUL_REQUESTS": 4},
        ]
        connection = MagicMock()
        connection.cursor.return_value = cursor
        with patch.object(security, "get_connection", return_value=connection):
            slot = security.acquire_ai_quota_slot(7, 500)
        self.assertEqual(4, slot.used_requests)
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertIn("GET_LOCK", statements[0])
        self.assertIn("DATE_FORMAT(UTC_TIMESTAMP()", statements[1])
        self.assertIn("INTERVAL 1 MONTH", statements[1])
        slot.close()

    def test_quota_and_concurrency_fail_before_provider_work(self):
        for first_row, error_type in (
            ({"acquired": 0}, security.AIRequestInProgress),
            ({"acquired": 1}, security.AIQuotaExceeded),
        ):
            cursor = MagicMock()
            if error_type is security.AIQuotaExceeded:
                cursor.fetchone.side_effect = [
                    first_row, {"successful_requests": 500}
                ]
            else:
                cursor.fetchone.return_value = first_row
            connection = MagicMock()
            connection.cursor.return_value = cursor
            with patch.object(
                security, "get_connection", return_value=connection
            ):
                with self.assertRaises(error_type):
                    security.acquire_ai_quota_slot(7, 500)

    def test_non_positive_monthly_limit_is_rejected(self):
        app = Flask(__name__)
        for value in (0, -1, None, True):
            app.config["MAX_AI_REQUESTS_PER_MONTH"] = value
            with self.subTest(value=value):
                with self.assertRaises(RuntimeError):
                    security.validate_sync_ai_security_config(app)


class SyncAIEndpointTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__, template_folder="../app/templates")
        app.config.update(
            TESTING=True,
            SECRET_KEY="test-secret",
            MAX_AI_REQUESTS_PER_MONTH=500,
        )
        init_csrf(app)
        app.register_blueprint(analysis_bp)
        self.app = app
        self.client = app.test_client()

    def _session(self, user_id=7, role="admin"):
        with self.client.session_transaction() as flask_session:
            flask_session["user_id"] = user_id
            flask_session["role"] = role
            flask_session["_csrf_token"] = "csrf-test-token"

    def _post(self, endpoint, text="A useful review", **kwargs):
        return self.client.post(
            endpoint,
            data={"review_text": text, "csrf_token": "csrf-test-token"},
            environ_base={"REMOTE_ADDR": kwargs.get("ip", "198.51.100.10")},
        )

    @staticmethod
    def _result(operation):
        data = (
            {
                "sentiment": "positive",
                "positives": ["Helpful"],
                "issues": [],
                "summary": "Good",
            }
            if operation == "review_assistant_analysis"
            else {"sentiment": "positive", "reply": "Thank you"}
        )
        return AIResult(
            data=data,
            provider="gemini",
            model_name="test",
            operation_type=operation,
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            estimated_cost=0,
            request_status="success",
            response_time_ms=1,
            error_message=None,
        )

    def _successful_patches(self):
        slot = MagicMock()
        slot.cursor = MagicMock()
        slot.connection = MagicMock()
        return slot, (
            patch(
                "app.routes.analysis.consume_ai_rate_limits",
                return_value=(None, None),
            ),
            patch(
                "app.routes.analysis.acquire_ai_quota_slot",
                return_value=slot,
            ),
            patch(
                "app.routes.analysis.ai_service.generate_json",
                side_effect=lambda _prompt, operation: self._result(operation),
            ),
            patch("app.routes.analysis.log_ai_usage"),
        )

    def test_unauthenticated_and_csrf_requests_are_rejected(self):
        response = self.client.post(
            "/review-assistant/analyze", data={"review_text": "hello"}
        )
        self.assertEqual(302, response.status_code)
        self._session()
        response = self.client.post(
            "/review-assistant/analyze", data={"review_text": "hello"}
        )
        self.assertEqual(403, response.status_code)

    def test_inactive_subscription_is_rejected(self):
        self._session(role="user")
        with patch(
            "app.services.subscription_service.has_active_subscription",
            return_value=False,
        ):
            response = self._post("/review-assistant/analyze")
        self.assertEqual(302, response.status_code)
        self.assertIn("/pricing", response.headers["Location"])

    def test_input_rejection_precedes_limiter_and_never_echoes_text(self):
        self._session()
        secret_text = "do-not-log\x00secret"
        with self.assertLogs("app.routes.analysis", logging.WARNING) as logs:
            logging.getLogger("app.routes.analysis").warning("test boundary")
            with patch(
                "app.routes.analysis.consume_ai_rate_limits"
            ) as limiter:
                response = self._post(
                    "/review-assistant/analyze", secret_text
                )
        self.assertEqual(400, response.status_code)
        self.assertNotIn(secret_text, response.get_data(as_text=True))
        self.assertNotIn(secret_text, " ".join(logs.output))
        limiter.assert_not_called()

    def test_exact_limit_unicode_and_normal_response_shapes(self):
        self._session()
        slot, patches = self._successful_patches()
        for context in patches:
            context.start()
            self.addCleanup(context.stop)
        for endpoint, marker in (
            ("/review-assistant/analyze", "AI findings"),
            ("/review-assistant/reply", "Suggested response"),
        ):
            response = self._post(endpoint, "é" * 5000)
            self.assertEqual(200, response.status_code)
            self.assertIn(marker, response.get_data(as_text=True))
        self.assertEqual(2, slot.connection.commit.call_count)

    def test_rate_limit_returns_429_retry_after_and_skips_gemini(self):
        self._session()
        status = LimitStatus(True, 11, 321)
        with patch(
            "app.routes.analysis.consume_ai_rate_limits",
            return_value=("user", status),
        ), patch(
            "app.routes.analysis.ai_service.generate_json"
        ) as gemini:
            response = self._post("/review-assistant/reply")
        self.assertEqual(429, response.status_code)
        self.assertEqual("321", response.headers["Retry-After"])
        gemini.assert_not_called()

    def test_limiter_failure_fails_closed_and_skips_gemini(self):
        self._session()
        with patch(
            "app.routes.analysis.consume_ai_rate_limits",
            side_effect=security.AISecurityUnavailable(),
        ), patch(
            "app.routes.analysis.ai_service.generate_json"
        ) as gemini:
            response = self._post("/review-assistant/analyze")
        self.assertEqual(503, response.status_code)
        gemini.assert_not_called()

    def test_quota_and_concurrency_rejections_skip_gemini(self):
        self._session()
        for error, expected_text in (
            (security.AIQuotaExceeded(), "monthly AI request quota"),
            (security.AIRequestInProgress(), "already in progress"),
        ):
            with self.subTest(error=type(error).__name__), patch(
                "app.routes.analysis.consume_ai_rate_limits",
                return_value=(None, None),
            ), patch(
                "app.routes.analysis.acquire_ai_quota_slot",
                side_effect=error,
            ), patch(
                "app.routes.analysis.ai_service.generate_json"
            ) as gemini:
                response = self._post("/review-assistant/reply")
                self.assertEqual(429, response.status_code)
                self.assertIn(expected_text, response.get_data(as_text=True))
                gemini.assert_not_called()

    def test_provider_error_is_generic_and_usage_is_not_recorded(self):
        self._session()
        slot = MagicMock()
        slot.cursor = MagicMock()
        slot.connection = MagicMock()
        with patch(
            "app.routes.analysis.consume_ai_rate_limits",
            return_value=(None, None),
        ), patch(
            "app.routes.analysis.acquire_ai_quota_slot",
            return_value=slot,
        ), patch(
            "app.routes.analysis.ai_service.generate_json",
            side_effect=RuntimeError("api_key=secret raw provider body"),
        ), patch(
            "app.routes.analysis.log_ai_usage"
        ) as usage:
            response = self._post("/review-assistant/analyze")
        body = response.get_data(as_text=True)
        self.assertEqual(503, response.status_code)
        self.assertNotIn("secret", body)
        self.assertNotIn("provider body", body)
        usage.assert_not_called()
        slot.connection.rollback.assert_called_once_with()
        slot.close.assert_called_once_with()

    def test_frontend_bounds_text_and_disables_repeat_submit(self):
        self._session()
        response = self.client.get("/review-assistant")
        body = response.get_data(as_text=True)
        self.assertIn('maxlength="5000"', body)
        self.assertIn('id="reviewAssistantForm"', body)
        self.assertIn("button.disabled = true", body)


if __name__ == "__main__":
    unittest.main()
