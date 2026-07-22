"""Opt-in real-MySQL tests for the reusable limiter backend.

Set TEST_MYSQL_DATABASE (ending in ``_test``) and TEST_MYSQL_HOST/PORT/USER/
PASSWORD.  This module creates and drops only ``rate_limit_counters``.
"""

import os
import re
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import mysql.connector

from app.config import Config
from app.services.limiter_service import LimiterService, MySQLLimiterBackend


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "database/migrations/20260722_001_create_rate_limit_counters.sql"


def _settings():
    database = os.getenv("TEST_MYSQL_DATABASE")
    if not database:
        raise unittest.SkipTest("Set TEST_MYSQL_DATABASE to run limiter MySQL tests.")
    if not re.fullmatch(r"[A-Za-z0-9_]+", database) or not database.lower().endswith("_test"):
        raise RuntimeError("TEST_MYSQL_DATABASE must be a safe name ending in '_test'.")
    if database in {Config.DB_NAME, os.getenv("MYSQL_DATABASE"), "reputation_db"}:
        raise RuntimeError("Limiter integration tests require a separate test database.")
    return {
        "host": os.getenv("TEST_MYSQL_HOST", "127.0.0.1"),
        "port": int(os.getenv("TEST_MYSQL_PORT", "3306")),
        "user": os.getenv("TEST_MYSQL_USER", "root"),
        "password": os.getenv("TEST_MYSQL_PASSWORD", ""),
        "database": database,
    }


class MySQLLimiterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = _settings()
        connection = mysql.connector.connect(**cls.settings)
        cursor = connection.cursor()
        try:
            cursor.execute("DROP TABLE IF EXISTS rate_limit_counters")
            cursor.execute(MIGRATION.read_text(encoding="utf-8"))
            connection.commit()
        finally:
            cursor.close()
            connection.close()

    @classmethod
    def tearDownClass(cls):
        connection = mysql.connector.connect(**cls.settings)
        cursor = connection.cursor()
        try:
            cursor.execute("DROP TABLE IF EXISTS rate_limit_counters")
            connection.commit()
        finally:
            cursor.close()
            connection.close()

    def setUp(self):
        connection = self.connect()
        cursor = connection.cursor()
        cursor.execute("TRUNCATE TABLE rate_limit_counters")
        cursor.close()
        connection.close()
        self.service = LimiterService(MySQLLimiterBackend(self.connect))

    def connect(self):
        return mysql.connector.connect(**self.settings)

    def execute(self, sql, params=(), *, dictionary=False):
        connection = self.connect()
        cursor = connection.cursor(dictionary=dictionary)
        try:
            cursor.execute(sql, params)
            result = cursor.fetchall() if cursor.with_rows else cursor.rowcount
            connection.commit()
            return result
        finally:
            cursor.close()
            connection.close()

    def record(self, scope="account", key="user@example.com", threshold=100):
        return self.service.record_failure(
            scope, key, threshold=threshold, window_seconds=60, block_seconds=120,
        )

    def test_atomic_increment_and_blocking_logic(self):
        self.assertEqual(1, self.record(threshold=3).attempt_count)
        self.assertFalse(self.record(threshold=3).blocked)
        third = self.record(threshold=3)
        self.assertTrue(third.blocked)
        self.assertEqual(3, third.attempt_count)
        self.assertGreater(third.retry_after_seconds, 0)
        self.assertTrue(self.service.check_limit("account", "user@example.com").blocked)

    def test_concurrent_updates_have_no_lost_increments_or_duplicate_rows(self):
        workers = 16
        barrier = threading.Barrier(workers)

        def concurrent_failure(_):
            barrier.wait()
            return self.record(threshold=1000)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(concurrent_failure, range(workers)))
        rows = self.execute(
            "SELECT COUNT(*) AS rows_found, MAX(attempt_count) AS attempts "
            "FROM rate_limit_counters", dictionary=True,
        )
        self.assertEqual({"rows_found": 1, "attempts": workers}, rows[0])

    def test_window_and_block_expiry_reset_counter(self):
        self.record(threshold=2)
        self.execute(
            "UPDATE rate_limit_counters SET window_started_at="
            "DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 61 SECOND)"
        )
        self.assertEqual(1, self.record(threshold=2).attempt_count)
        blocked = self.record(threshold=2)
        self.assertTrue(blocked.blocked)
        self.execute(
            "UPDATE rate_limit_counters SET blocked_until="
            "DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 1 SECOND)"
        )
        status = self.service.check_limit("account", "user@example.com")
        self.assertFalse(status.blocked)
        self.assertEqual(0, status.attempt_count)

    def test_reset_and_multiple_scopes_are_independent(self):
        cases = (("ip", "127.0.0.1"), ("account", "user@example.com"),
                 ("ip_account", ("127.0.0.1", "user@example.com")))
        for scope, key in cases:
            self.record(scope, key)
        self.assertEqual(3, self.execute("SELECT COUNT(*) AS n FROM rate_limit_counters", dictionary=True)[0]["n"])
        self.assertTrue(self.service.reset(*cases[0]))
        self.assertFalse(self.service.reset(*cases[0]))
        self.assertEqual(2, self.execute("SELECT COUNT(*) AS n FROM rate_limit_counters", dictionary=True)[0]["n"])

    def test_cleanup_is_bounded_and_preserves_active_blocks(self):
        for number in range(3):
            self.record("account", f"old-{number}@example.com")
        self.execute(
            "UPDATE rate_limit_counters SET updated_at="
            "DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 2 DAY)"
        )
        self.record("account", "blocked@example.com", threshold=1)
        self.execute(
            "UPDATE rate_limit_counters SET updated_at="
            "DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 2 DAY) WHERE blocked_until IS NOT NULL"
        )
        self.assertEqual(2, self.service.cleanup(older_than_seconds=86400, limit=2))
        self.assertEqual(1, self.service.cleanup(older_than_seconds=86400, limit=10))
        self.assertTrue(self.service.check_limit("account", "blocked@example.com").blocked)

    def test_database_timestamps_are_utc_and_schema_stores_only_hash(self):
        before = datetime.now(timezone.utc).replace(tzinfo=None)
        self.record()
        after = datetime.now(timezone.utc).replace(tzinfo=None)
        row = self.execute(
            "SELECT scope,key_hash,window_started_at,created_at,updated_at "
            "FROM rate_limit_counters", dictionary=True,
        )[0]
        self.assertEqual("account", row["scope"])
        self.assertEqual(32, len(row["key_hash"]))
        for column in ("window_started_at", "created_at", "updated_at"):
            self.assertLessEqual(before, row[column])
            self.assertLessEqual(row[column], after)


if __name__ == "__main__":
    unittest.main()
