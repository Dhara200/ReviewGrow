import os
import unittest
from unittest.mock import patch

from tests.integration.test_limiter_mysql import _settings


class LimiterMySQLSafetyTests(unittest.TestCase):
    def valid_environment(self):
        return {
            "TEST_MYSQL_HOST": "127.0.0.1",
            "TEST_MYSQL_PORT": "3306",
            "TEST_MYSQL_USER": "limiter_test",
            "TEST_MYSQL_PASSWORD": "not-printed",
            "TEST_MYSQL_DATABASE": "reviewgrow_limiter_test",
        }

    def test_absent_database_configuration_skips(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(unittest.SkipTest):
            _settings()

    def test_every_test_setting_must_be_explicit(self):
        for missing in self.valid_environment():
            environment = self.valid_environment()
            environment.pop(missing)
            with self.subTest(missing=missing), patch.dict(
                os.environ, environment, clear=True
            ), self.assertRaises((RuntimeError, unittest.SkipTest)):
                _settings()

    def test_unsafe_database_names_are_rejected(self):
        for database in (
            "reputation_db", "reviewgrow", "production", "prod",
            "reputation_db_test", "reviewgrow_test", "production_test",
            "prod_testing", "unsafe-name_test", "ordinary_database",
        ):
            environment = self.valid_environment()
            environment["TEST_MYSQL_DATABASE"] = database
            with self.subTest(database=database), patch.dict(
                os.environ, environment, clear=True
            ), self.assertRaises(RuntimeError):
                _settings()

    def test_valid_settings_do_not_fall_back_to_application_values(self):
        environment = self.valid_environment()
        with patch.dict(os.environ, environment, clear=True):
            settings = _settings()
        self.assertEqual("reviewgrow_limiter_test", settings["database"])
        self.assertEqual("limiter_test", settings["user"])


if __name__ == "__main__":
    unittest.main()
