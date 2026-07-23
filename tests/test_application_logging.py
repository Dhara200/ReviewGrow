import importlib
import logging
import sys
import unittest
from unittest.mock import patch

from app.config import Config
from app.services.security_audit_service import SecurityAuditService


AUDIT_KEY = "application-logging-test-key-0123456789abcdef"


class ApplicationLoggingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Import the real Gunicorn target while preventing its unrelated startup
        # schema compatibility check from opening a database connection.
        sys.modules.pop("app.app", None)
        cls.schema_patch = patch(
            "app.services.schema_compatibility_service.validate_runtime_schema",
            return_value=None,
        )
        cls.schema_patch.start()
        cls.config_patches = (
            patch.object(Config, "APP_ENV", "production"),
            patch.object(Config, "SECRET_KEY", "s" * 32),
            patch.object(Config, "SESSION_COOKIE_SECURE", True),
            patch.object(Config, "SECURITY_AUDIT_ENABLED", True),
            patch.object(Config, "SECURITY_AUDIT_HMAC_KEY", AUDIT_KEY),
        )
        for active_patch in cls.config_patches:
            active_patch.start()
        cls.module = importlib.import_module("app.app")
        cls.app = cls.module.app

    @classmethod
    def tearDownClass(cls):
        for active_patch in reversed(cls.config_patches):
            active_patch.stop()
        cls.schema_patch.stop()

    def test_production_application_logger_effective_level_is_info(self):
        self.assertEqual(logging.INFO, self.app.logger.level)
        self.assertEqual(logging.INFO, self.app.logger.getEffectiveLevel())
        self.assertFalse(self.app.debug)

    def test_info_success_and_warning_failure_events_are_not_filtered(self):
        records = []

        class CaptureHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = CaptureHandler()
        handler.setLevel(logging.NOTSET)
        self.app.logger.addHandler(handler)
        try:
            audit = SecurityAuditService(
                self.app.logger, enabled=True, hmac_key=AUDIT_KEY
            )
            audit.emit(
                "login_success", email="owner@example.com",
                client_ip="198.51.100.10", http_status=302,
            )
            audit.emit(
                "login_invalid_credentials", email="owner@example.com",
                client_ip="198.51.100.10", http_status=401,
            )
        finally:
            self.app.logger.removeHandler(handler)
            handler.close()

        audit_records = [
            record for record in records
            if record.getMessage().startswith(SecurityAuditService.PREFIX)
        ]
        self.assertEqual([logging.INFO, logging.WARNING], [
            record.levelno for record in audit_records
        ])
        self.assertIn('"event_name":"login_success"', audit_records[0].getMessage())
        self.assertIn(
            '"event_name":"login_invalid_credentials"',
            audit_records[1].getMessage(),
        )

    def test_reinitialization_does_not_add_duplicate_handlers(self):
        handlers_before = tuple(self.app.logger.handlers)
        reloaded = importlib.reload(self.module)
        handlers_after = tuple(reloaded.app.logger.handlers)
        self.assertEqual(handlers_before, handlers_after)
        self.assertEqual(len(handlers_before), len(set(map(id, handlers_after))))
        self.assertEqual(logging.INFO, reloaded.app.logger.getEffectiveLevel())


if __name__ == "__main__":
    unittest.main()
