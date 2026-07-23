import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.config import Config
from app.services import database_service
from app.services import schema_compatibility_service as schema


class SchemaCompatibilityServiceTests(unittest.TestCase):
    def setUp(self):
        schema._reset_validation_cache_for_tests()

    def _valid_rows(self):
        return [
            {"table_name": table, "column_name": column}
            for table, columns in schema.REQUIRED_SCHEMA.items()
            for column in columns
        ]

    def _connection(self, rows):
        cursor = MagicMock()
        cursor.fetchall.return_value = rows
        connection = MagicMock()
        connection.cursor.return_value = cursor
        return connection, cursor

    def test_valid_schema_allows_startup_with_read_only_metadata_query(self):
        connection, cursor = self._connection(self._valid_rows())

        with patch.object(database_service, "get_connection", return_value=connection):
            schema.validate_runtime_schema()

        sql = cursor.execute.call_args.args[0].upper()
        self.assertIn("INFORMATION_SCHEMA.COLUMNS", sql)
        for ddl in ("CREATE ", "ALTER ", "DROP ", "TRUNCATE ", "RENAME "):
            self.assertNotIn(ddl, sql)
        connection.commit.assert_not_called()
        connection.rollback.assert_not_called()
        cursor.close.assert_called_once_with()
        connection.close.assert_called_once_with()

    def test_missing_required_schema_fails_predictably(self):
        rows = self._valid_rows()
        rows = [
            row for row in rows
            if not (
                row["table_name"] == "users"
                and row["column_name"] == "password_hash"
            )
        ]
        connection, _ = self._connection(rows)

        with patch.object(database_service, "get_connection", return_value=connection):
            with self.assertRaisesRegex(
                schema.SchemaCompatibilityError,
                "apply pending migrations.*users.password_hash",
            ):
                schema.validate_runtime_schema()

    def test_database_failure_is_sanitized_and_not_ignored(self):
        secret = "production-password=do-not-expose"
        raw_sql = "SELECT * FROM private_credentials"
        failure = RuntimeError(f"{secret}; failed SQL: {raw_sql}")

        with patch.object(database_service, "get_connection", side_effect=failure):
            with self.assertRaises(schema.SchemaCompatibilityError) as raised:
                schema.validate_runtime_schema()

        message = str(raised.exception)
        self.assertEqual(
            message,
            "Database unavailable during schema compatibility check.",
        )
        self.assertNotIn(secret, message)
        self.assertNotIn(raw_sql, message)

    def test_validation_runs_only_once_per_process(self):
        connection, cursor = self._connection(self._valid_rows())

        with patch.object(
            database_service, "get_connection", return_value=connection
        ) as get_connection:
            schema.validate_runtime_schema()
            schema.validate_runtime_schema()

        get_connection.assert_called_once_with()
        cursor.execute.assert_called_once()

    def test_failed_validation_is_not_cached_and_next_attempt_retries(self):
        valid_connection, _ = self._connection(self._valid_rows())

        with patch.object(
            database_service,
            "get_connection",
            side_effect=[
                RuntimeError("temporary database failure"),
                valid_connection,
            ],
        ) as get_connection:
            with self.assertRaises(schema.SchemaCompatibilityError):
                schema.validate_runtime_schema()
            self.assertFalse(schema._validated)

            schema.validate_runtime_schema()

        self.assertTrue(schema._validated)
        self.assertEqual(get_connection.call_count, 2)

    def test_legacy_schema_initializer_has_been_removed(self):
        self.assertFalse(hasattr(database_service, "ensure_mvp_schema"))


class RuntimeStartupTests(unittest.TestCase):
    def test_app_imports_and_reloads_never_call_legacy_ddl(self):
        sys.modules.pop("app.app", None)

        with patch.object(
            Config, "APP_ENV", "production"
        ), patch.object(Config, "SECRET_KEY", "s" * 32), patch.object(
            Config, "SESSION_COOKIE_SECURE", True
        ), patch.object(
            Config, "SECURITY_AUDIT_ENABLED", True
        ), patch.object(
            Config,
            "SECURITY_AUDIT_HMAC_KEY",
            "startup-schema-audit-key-0123456789abcdef",
        ), patch.object(
            schema, "validate_runtime_schema", return_value=None
        ) as validate:
            module = importlib.import_module("app.app")
            importlib.reload(module)

        self.assertEqual(validate.call_count, 2)

    def test_failed_app_import_exposes_no_requestable_application_and_retries(self):
        sys.modules.pop("app.app", None)
        startup_error = schema.SchemaCompatibilityError("safe schema failure")

        with patch.object(Config, "APP_ENV", "production"), patch.object(
            Config, "SECRET_KEY", "s" * 32
        ), patch.object(Config, "SESSION_COOKIE_SECURE", True), patch.object(
            Config, "SECURITY_AUDIT_ENABLED", True
        ), patch.object(
            Config,
            "SECURITY_AUDIT_HMAC_KEY",
            "startup-schema-audit-key-0123456789abcdef",
        ), patch.object(
            schema,
            "validate_runtime_schema",
            side_effect=[startup_error, None],
        ) as validate:
            with self.assertRaisesRegex(
                schema.SchemaCompatibilityError, "safe schema failure"
            ):
                importlib.import_module("app.app")

            self.assertNotIn("app.app", sys.modules)
            module = importlib.import_module("app.app")

        self.assertIsNotNone(module.app)
        self.assertEqual(validate.call_count, 2)

    def test_configuration_is_loaded_before_web_and_worker_schema_checks(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "app" / "app.py").read_text(encoding="utf-8")
        worker_source = (root / "worker.py").read_text(encoding="utf-8")

        self.assertLess(
            app_source.index("app.config.from_object(Config)"),
            app_source.index("validate_runtime_schema()"),
        )
        self.assertLess(
            worker_source.index("from app.config import Config"),
            worker_source.index("validate_runtime_schema()"),
        )
        self.assertLess(
            worker_source.index("validate_runtime_schema()"),
            worker_source.index('print("Background worker started."'),
        )

    def test_worker_startup_checks_schema_then_runs_without_ddl(self):
        import worker

        order = []
        with patch.object(
            worker, "_install_signal_handlers", side_effect=lambda: order.append("signals")
        ), patch.object(
            worker, "validate_runtime_schema", side_effect=lambda: order.append("schema")
        ), patch.object(
            worker, "run_worker_forever", side_effect=lambda: order.append("run")
        ):
            worker.main()

        self.assertEqual(order, ["signals", "schema", "run"])

    def test_worker_does_not_start_when_schema_check_fails(self):
        import worker

        with patch.object(worker, "_install_signal_handlers"), patch.object(
            worker,
            "validate_runtime_schema",
            side_effect=schema.SchemaCompatibilityError("safe startup failure"),
        ), patch.object(worker, "run_worker_forever") as run:
            with self.assertRaisesRegex(
                schema.SchemaCompatibilityError, "safe startup failure"
            ):
                worker.main()

        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
