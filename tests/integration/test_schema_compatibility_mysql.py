import os
import unittest
from unittest.mock import patch

from app.config import Config
from app.services import database_service
from app.services import schema_compatibility_service as schema


def _settings():
    names = (
        "TEST_MYSQL_HOST",
        "TEST_MYSQL_PORT",
        "TEST_MYSQL_USER",
        "TEST_MYSQL_PASSWORD",
        "TEST_MYSQL_DATABASE",
    )
    values = {name: os.getenv(name) for name in names}
    if not all(values.values()):
        raise unittest.SkipTest(
            "Set all TEST_MYSQL_* variables to run schema compatibility MySQL tests."
        )

    database = values["TEST_MYSQL_DATABASE"]
    if not database.lower().endswith(("_test", "_testing")):
        raise RuntimeError("TEST_MYSQL_DATABASE must be a dedicated test database.")
    protected = {
        str(value).casefold()
        for value in (Config.DB_NAME, os.getenv("MYSQL_DATABASE"))
        if value
    }
    if database.casefold() in protected:
        raise RuntimeError("TEST_MYSQL_DATABASE must not match an application database.")

    return {
        "host": values["TEST_MYSQL_HOST"],
        "port": int(values["TEST_MYSQL_PORT"]),
        "user": values["TEST_MYSQL_USER"],
        "password": values["TEST_MYSQL_PASSWORD"],
        "database": database,
    }


class RecordingCursor:
    def __init__(self, cursor, statements):
        self._cursor = cursor
        self._statements = statements

    def execute(self, statement, params=None):
        self._statements.append(statement)
        return self._cursor.execute(statement, params)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def close(self):
        return self._cursor.close()


class RecordingConnection:
    def __init__(self, connection):
        self._connection = connection
        self.statements = []

    def cursor(self, *args, **kwargs):
        return RecordingCursor(
            self._connection.cursor(*args, **kwargs),
            self.statements,
        )

    def close(self):
        return self._connection.close()

    def commit(self):
        raise AssertionError("Schema compatibility validation must not commit.")

    def rollback(self):
        raise AssertionError("Schema compatibility validation must not roll back.")


class SchemaCompatibilityMySQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = _settings()

    def setUp(self):
        schema._reset_validation_cache_for_tests()

    def _connect(self):
        with patch.object(Config, "DB_HOST", self.settings["host"]), patch.object(
            Config, "DB_PORT", self.settings["port"]
        ), patch.object(Config, "DB_USER", self.settings["user"]), patch.object(
            Config, "DB_PASSWORD", self.settings["password"]
        ), patch.object(Config, "DB_NAME", self.settings["database"]):
            return database_service.get_connection()

    def test_real_mysql_metadata_discovery_is_read_only_and_shape_safe(self):
        connection = self._connect()
        tuple_cursor = connection.cursor()
        tuple_cursor.execute("SELECT DATABASE() AS active_database")
        active_tuple = tuple_cursor.fetchone()
        tuple_cursor.close()
        self.assertEqual(
            schema._row_values(active_tuple, ("active_database",))[0],
            self.settings["database"],
        )

        dictionary_cursor = connection.cursor(dictionary=True)
        dictionary_cursor.execute(
            """
            SELECT TABLE_NAME AS table_name, COLUMN_NAME AS column_name
            FROM information_schema.columns
            WHERE TABLE_SCHEMA=%s
            """,
            (self.settings["database"],),
        )
        dictionary_rows = dictionary_cursor.fetchall()
        dictionary_cursor.execute(
            """
            SELECT TABLE_NAME AS table_name, COLUMN_NAME AS column_name
            FROM information_schema.columns
            WHERE TABLE_SCHEMA=%s
            """,
            ("__schema_that_does_not_exist__",),
        )
        wrong_schema_rows = dictionary_cursor.fetchall()
        dictionary_cursor.close()
        connection.close()

        self.assertTrue(dictionary_rows)
        discovered = {
            schema._row_values(row, ("table_name", "column_name"))
            for row in dictionary_rows
        }
        expected = {
            (table, column)
            for table, columns in schema.REQUIRED_SCHEMA.items()
            for column in columns
        }
        self.assertTrue(expected.issubset(discovered))

        self.assertEqual(wrong_schema_rows, [])
        self.assertTrue(schema._missing_required_metadata(wrong_schema_rows))

        one_missing = [
            row
            for row in dictionary_rows
            if schema._row_values(row, ("table_name", "column_name"))
            != ("businesses", "id")
        ]
        self.assertEqual(
            schema._missing_required_metadata(one_missing),
            ["businesses.id"],
        )

        recorded = RecordingConnection(self._connect())
        with patch.object(
            database_service,
            "get_connection",
            return_value=recorded,
        ):
            schema.validate_runtime_schema()

        self.assertTrue(schema._validated)
        self.assertEqual(len(recorded.statements), 2)
        self.assertTrue(
            all(statement.lstrip().upper().startswith("SELECT") for statement in recorded.statements)
        )


if __name__ == "__main__":
    unittest.main()
