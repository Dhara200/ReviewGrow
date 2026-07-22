"""Opt-in real-MySQL tests for the reusable limiter backend.

Set TEST_MYSQL_DATABASE (ending in ``_test``) and TEST_MYSQL_HOST/PORT/USER/
PASSWORD.  This module creates and drops only ``rate_limit_counters``.
"""

import os
import re
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import mysql.connector
from flask import Flask
from werkzeug.security import generate_password_hash

from app.config import Config
from app.routes.auth import auth_bp
from app.services.csrf_service import init_csrf
from app.services.limiter_service import LimiterService, MySQLLimiterBackend, hash_key
from app.services.login_limiter_service import LoginLimiter, LoginLimiterPolicy
from app.services.security_audit_service import SecurityAuditService


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "database/migrations/20260722_001_create_rate_limit_counters.sql"
AUDIT_TEST_KEY = "mysql-login-audit-test-key-0123456789abcdef"


class _AuthCursor:
    def __init__(self, user=None):
        self.user = user

    def execute(self, query, params):
        pass

    def fetchone(self):
        return self.user

    def close(self):
        pass


class _AuthConnection:
    def __init__(self, user=None):
        self.cursor_instance = _AuthCursor(user)

    def cursor(self, dictionary=False):
        return self.cursor_instance

    def rollback(self):
        pass

    def close(self):
        pass


def _settings():
    required = (
        "TEST_MYSQL_HOST", "TEST_MYSQL_PORT", "TEST_MYSQL_USER",
        "TEST_MYSQL_PASSWORD", "TEST_MYSQL_DATABASE",
    )
    database = os.getenv("TEST_MYSQL_DATABASE")
    if not database:
        raise unittest.SkipTest("Set TEST_MYSQL_DATABASE to run limiter MySQL tests.")
    missing = [name for name in required if name not in os.environ]
    if missing:
        raise RuntimeError(
            "All TEST_MYSQL_* variables must be explicitly configured; missing: "
            + ", ".join(missing)
        )
    lowered = database.casefold()
    suffix = next((value for value in ("_testing", "_test") if lowered.endswith(value)), None)
    if not re.fullmatch(r"[A-Za-z0-9_]+", database) or suffix is None:
        raise RuntimeError("TEST_MYSQL_DATABASE must be a safe name ending in '_test' or '_testing'.")
    stem = lowered[:-len(suffix)].rstrip("_")
    unsafe_stems = {"reputation_db", "reviewgrow", "production", "prod", "development", "dev"}
    if stem in unsafe_stems or "production" in stem or re.search(r"(^|_)prod($|_)", stem):
        raise RuntimeError("TEST_MYSQL_DATABASE resembles a production/development database.")
    normal_names = {
        str(value).casefold() for value in (Config.DB_NAME, os.getenv("MYSQL_DATABASE")) if value
    }
    if lowered in normal_names:
        raise RuntimeError("Limiter integration tests require a separate test database.")
    if not os.getenv("TEST_MYSQL_HOST") or not os.getenv("TEST_MYSQL_USER"):
        raise RuntimeError("TEST_MYSQL_HOST and TEST_MYSQL_USER must be non-empty.")
    try:
        port = int(os.environ["TEST_MYSQL_PORT"])
    except ValueError as error:
        raise RuntimeError("TEST_MYSQL_PORT must be an integer.") from error
    if not 1 <= port <= 65535:
        raise RuntimeError("TEST_MYSQL_PORT must be between 1 and 65535.")
    return {
        "host": os.environ["TEST_MYSQL_HOST"],
        "port": port,
        "user": os.environ["TEST_MYSQL_USER"],
        "password": os.environ["TEST_MYSQL_PASSWORD"],
        "database": database,
    }


class MySQLLimiterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = _settings()
        print(f"Running limiter MySQL tests in isolated database: {cls.settings['database']}")
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

    def test_real_migration_schema_and_mysql_version(self):
        version = self.execute("SELECT VERSION() AS version", dictionary=True)[0]["version"]
        self.assertTrue(version)
        columns = self.execute(
            "SELECT column_name,column_type FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name='rate_limit_counters'",
            (self.settings["database"],), dictionary=True,
        )
        self.assertEqual(
            {"id", "scope", "key_hash", "window_started_at", "attempt_count",
             "blocked_until", "created_at", "updated_at"},
            {row["column_name"] for row in columns},
        )
        key_column = next(row for row in columns if row["column_name"] == "key_hash")
        self.assertEqual("binary(32)", key_column["column_type"].lower())

    def test_basic_persistence_independent_keys_and_no_raw_identity_storage(self):
        first = self.record("account", "first@example.com")
        second = self.record("account", "first@example.com")
        self.record("account", "second@example.com")
        self.record("ip", "198.51.100.8")
        self.assertEqual((1, 2), (first.attempt_count, second.attempt_count))
        rows = self.execute(
            "SELECT scope,key_hash,attempt_count,HEX(key_hash) AS key_hex "
            "FROM rate_limit_counters ORDER BY id", dictionary=True,
        )
        self.assertEqual(3, len(rows))
        self.assertTrue(all(len(row["key_hash"]) == 32 for row in rows))
        self.assertEqual(
            hash_key("account", "first@example.com").hex().upper(), rows[0]["key_hex"]
        )
        rendered = repr(rows).lower()
        self.assertNotIn("first@example.com", rendered)
        self.assertNotIn("198.51.100.8", rendered)

    def test_concurrent_updates_have_no_lost_increments_or_duplicate_rows(self):
        workers = 50
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

    def test_concurrent_existing_row_and_threshold_boundary_across_instances(self):
        self.record(threshold=26)
        workers = 25
        barrier = threading.Barrier(workers)
        services = [LimiterService(MySQLLimiterBackend(self.connect)) for _ in range(workers)]

        def update(number):
            barrier.wait()
            return services[number].record_failure(
                "account", "user@example.com", threshold=26,
                window_seconds=60, block_seconds=120,
            )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            statuses = list(pool.map(update, range(workers)))
        row = self.execute(
            "SELECT COUNT(*) AS rows_found,attempt_count,blocked_until IS NOT NULL AS blocked "
            "FROM rate_limit_counters GROUP BY attempt_count,blocked_until", dictionary=True,
        )[0]
        self.assertEqual(1, row["rows_found"])
        self.assertEqual(26, row["attempt_count"])
        self.assertEqual(1, row["blocked"])
        self.assertTrue(any(status.blocked for status in statuses))
        self.assertTrue(self.service.check_limit("account", "user@example.com").blocked)

    def test_failed_commit_rolls_back_increment_and_releases_lock(self):
        self.record()
        real_connection = self.connect()

        class CommitFailureConnection:
            def cursor(self, *args, **kwargs):
                return real_connection.cursor(*args, **kwargs)

            def commit(self):
                raise RuntimeError("simulated commit failure")

            def rollback(self):
                real_connection.rollback()

            def close(self):
                real_connection.close()

        failing = LimiterService(MySQLLimiterBackend(CommitFailureConnection))
        with self.assertRaisesRegex(RuntimeError, "simulated commit failure"):
            failing.record_failure(
                "account", "user@example.com", threshold=100,
                window_seconds=60, block_seconds=120,
            )
        row = self.execute(
            "SELECT attempt_count FROM rate_limit_counters", dictionary=True
        )[0]
        self.assertEqual(1, row["attempt_count"])
        self.assertEqual(2, self.record().attempt_count)

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

    def test_layered_login_reset_and_shared_identity_patterns(self):
        policy = LoginLimiterPolicy(
            ip_threshold=100, ip_window_seconds=60, ip_block_seconds=120,
            account_threshold=100, account_window_seconds=60, account_block_seconds=120,
            ip_account_threshold=100, ip_account_window_seconds=60,
            ip_account_block_seconds=120,
        )
        login = LoginLimiter(policy, self.service)
        for email in ("one@example.com", "two@example.com", "three@example.com"):
            login.record_failure(email, "198.51.100.20")
        for number in range(3):
            login.record_failure("victim@example.com", f"203.0.113.{number + 1}")
        rows = self.execute(
            "SELECT scope,attempt_count,COUNT(*) AS row_count FROM rate_limit_counters "
            "GROUP BY scope,attempt_count ORDER BY scope,attempt_count", dictionary=True,
        )
        self.assertIn(
            {"scope": "ip", "attempt_count": 3, "row_count": 1}, rows
        )
        self.assertIn(
            {"scope": "account", "attempt_count": 3, "row_count": 1}, rows
        )
        login.reset_after_success("victim@example.com", "203.0.113.1")
        self.assertEqual(0, self.service.check_limit("account", "victim@example.com").attempt_count)
        self.assertEqual(
            0,
            self.service.check_limit(
                "ip_account", ("203.0.113.1", "victim@example.com")
            ).attempt_count,
        )
        self.assertEqual(1, self.service.check_limit("ip", "203.0.113.1").attempt_count)

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

    def test_cleanup_preserves_recent_rows_and_is_consistent_with_recording(self):
        self.record("account", "recent@example.com")
        self.record("account", "racing@example.com")
        self.execute(
            "UPDATE rate_limit_counters SET updated_at=DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 2 DAY) "
            "WHERE key_hash=%s", (hash_key("account", "racing@example.com"),)
        )
        barrier = threading.Barrier(2)
        results = []

        def cleanup():
            barrier.wait()
            results.append(self.service.cleanup(older_than_seconds=86400, limit=10))

        def record():
            barrier.wait()
            results.append(self.record("account", "racing@example.com").attempt_count)

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda fn: fn(), (cleanup, record)))
        self.assertEqual(1, self.service.check_limit("account", "recent@example.com").attempt_count)
        racing = self.service.check_limit("account", "racing@example.com")
        self.assertIn(racing.attempt_count, (1, 2))
        self.assertEqual(1, self.execute(
            "SELECT COUNT(*) AS n FROM rate_limit_counters WHERE key_hash=%s",
            (hash_key("account", "racing@example.com"),), dictionary=True,
        )[0]["n"])

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

    def test_session_timezone_does_not_change_utc_or_retry_semantics(self):
        def timezone_connection():
            connection = self.connect()
            cursor = connection.cursor()
            cursor.execute("SET time_zone='+05:30'")
            cursor.close()
            return connection

        service = LimiterService(MySQLLimiterBackend(timezone_connection))
        before = datetime.now(timezone.utc).replace(tzinfo=None)
        status = service.record_failure(
            "account", "timezone@example.com", threshold=1,
            window_seconds=60, block_seconds=120,
        )
        after = datetime.now(timezone.utc).replace(tzinfo=None)
        row = self.execute(
            "SELECT window_started_at FROM rate_limit_counters WHERE key_hash=%s",
            (hash_key("account", "timezone@example.com"),), dictionary=True,
        )[0]
        self.assertLessEqual(before, row["window_started_at"])
        self.assertLessEqual(row["window_started_at"], after)
        self.assertTrue(status.blocked)
        self.assertGreater(status.retry_after_seconds, 0)
        self.assertLessEqual(status.retry_after_seconds, 120)

    def test_bounded_performance_sanity(self):
        started = time.perf_counter()
        for _ in range(100):
            self.record("account", "sequential@example.com", threshold=1000)
        sequential_seconds = time.perf_counter() - started

        def concurrent_same(_):
            return self.record("account", "same@example.com", threshold=1000)

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=50) as pool:
            list(pool.map(concurrent_same, range(50)))
        same_seconds = time.perf_counter() - started

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=50) as pool:
            list(pool.map(
                lambda number: self.record(
                    "account", f"distinct-{number}@example.com", threshold=1000
                ),
                range(50),
            ))
        distinct_seconds = time.perf_counter() - started
        print(
            "Limiter performance sanity seconds: "
            f"sequential_100={sequential_seconds:.3f} "
            f"concurrent_same_50={same_seconds:.3f} "
            f"concurrent_distinct_50={distinct_seconds:.3f}"
        )

    def test_login_limiter_state_is_shared_across_instances_and_reinstantiation(self):
        policy = LoginLimiterPolicy(
            ip_threshold=2, ip_window_seconds=60, ip_block_seconds=120,
            account_threshold=3, account_window_seconds=60,
            account_block_seconds=120, ip_account_threshold=2,
            ip_account_window_seconds=60, ip_account_block_seconds=120,
        )
        first_app_limiter = LoginLimiter(
            policy, LimiterService(MySQLLimiterBackend(self.connect))
        )
        second_app_limiter = LoginLimiter(
            policy, LimiterService(MySQLLimiterBackend(self.connect))
        )
        first_app_limiter.record_failure("user@example.com", "198.51.100.9")
        second_app_limiter.record_failure("user@example.com", "198.51.100.9")

        # A newly constructed service represents a restarted Flask/Gunicorn process.
        restarted_limiter = LoginLimiter(
            policy, LimiterService(MySQLLimiterBackend(self.connect))
        )
        ip_status = restarted_limiter.check_ip("198.51.100.9")
        account_status, pair_status = restarted_limiter.check_account_and_pair(
            "user@example.com", "198.51.100.9"
        )
        self.assertTrue(ip_status.blocked)
        self.assertFalse(account_status.blocked)
        self.assertTrue(pair_status.blocked)

    def make_login_app(self, policy):
        app = Flask(__name__, template_folder="../../app/templates")
        app.config.update(
            TESTING=True,
            SECRET_KEY="mysql-login-integration",
            LOGIN_DUMMY_PASSWORD_HASH=Config.LOGIN_DUMMY_PASSWORD_HASH,
            SECURITY_AUDIT_ENABLED=True,
            SECURITY_AUDIT_HMAC_KEY=AUDIT_TEST_KEY,
        )
        init_csrf(app)
        app.register_blueprint(auth_bp)
        limiter = LoginLimiter(
            policy, LimiterService(MySQLLimiterBackend(self.connect))
        )
        return app, limiter

    @staticmethod
    def login_token(client):
        page = client.get("/login-page").get_data(as_text=True)
        return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)

    def login_post(self, client, *, email, ip, token, password="wrong"):
        return client.post(
            "/login-page",
            data={
                "email": email, "password": password, "csrf_token": token,
                "recaptcha_token": "test-provider-token",
            },
            environ_base={"REMOTE_ADDR": ip},
        )

    def test_real_mysql_login_boundary_restart_and_single_safe_audit_event(self):
        policy = LoginLimiterPolicy(
            ip_threshold=20, ip_window_seconds=900, ip_block_seconds=900,
            account_threshold=15, account_window_seconds=900,
            account_block_seconds=900, ip_account_threshold=5,
            ip_account_window_seconds=900, ip_account_block_seconds=900,
        )
        first_app, first_limiter = self.make_login_app(policy)
        first_client = first_app.test_client()
        token = self.login_token(first_client)
        captured_lines = []
        with patch("app.routes.auth._get_login_limiter", return_value=first_limiter), patch(
            "app.routes.auth.verify_recaptcha", return_value=SimpleNamespace(success=True, reason="")
        ), patch("app.routes.auth.get_connection", return_value=_AuthConnection()), patch(
            "app.routes.auth.check_password_hash", return_value=False
        ) as password_check:
            statuses = []
            for _ in range(5):
                with self.assertLogs(first_app.logger, level="WARNING") as captured:
                    response = self.login_post(
                        first_client, email="fixture@example.com",
                        ip="198.51.100.50", token=token,
                    )
                statuses.append(response.status_code)
                audit_lines = [
                    line for line in captured.output
                    if SecurityAuditService.PREFIX in line
                ]
                self.assertEqual(1, len(audit_lines))
                captured_lines.extend(audit_lines)
            self.assertEqual(5, password_check.call_count)

        second_app, second_limiter = self.make_login_app(policy)
        second_client = second_app.test_client()
        second_token = self.login_token(second_client)
        with patch("app.routes.auth._get_login_limiter", return_value=second_limiter), patch(
            "app.routes.auth.verify_recaptcha", return_value=SimpleNamespace(success=True, reason="")
        ), patch("app.routes.auth.get_connection") as user_lookup, patch(
            "app.routes.auth.check_password_hash"
        ) as password_check, self.assertLogs(second_app.logger, level="WARNING") as captured:
            blocked = self.login_post(
                second_client, email="fixture@example.com",
                ip="198.51.100.50", token=second_token,
            )
        self.assertEqual([401] * 5, statuses)
        self.assertEqual(429, blocked.status_code)
        self.assertTrue(blocked.headers["Retry-After"].isdigit())
        self.assertGreater(int(blocked.headers["Retry-After"]), 0)
        user_lookup.assert_not_called()
        password_check.assert_not_called()
        audit_lines = [line for line in captured.output if SecurityAuditService.PREFIX in line]
        self.assertEqual(1, len(audit_lines))
        all_logs = " ".join(captured_lines + audit_lines)
        self.assertNotIn("fixture@example.com", all_logs)
        self.assertNotIn("198.51.100.50", all_logs)

    def test_real_mysql_login_ip_and_account_layering(self):
        policy = LoginLimiterPolicy(
            ip_threshold=3, ip_window_seconds=900, ip_block_seconds=900,
            account_threshold=3, account_window_seconds=900,
            account_block_seconds=900, ip_account_threshold=20,
            ip_account_window_seconds=900, ip_account_block_seconds=900,
        )
        app, limiter = self.make_login_app(policy)
        client = app.test_client()
        token = self.login_token(client)
        patches = (
            patch("app.routes.auth._get_login_limiter", return_value=limiter),
            patch("app.routes.auth.verify_recaptcha", return_value=SimpleNamespace(success=True, reason="")),
            patch("app.routes.auth.get_connection", return_value=_AuthConnection()),
            patch("app.routes.auth.check_password_hash", return_value=False),
        )
        with patches[0], patches[1], patches[2], patches[3]:
            for number in range(3):
                self.assertEqual(401, self.login_post(
                    client, email=f"spray-{number}@example.com",
                    ip="198.51.100.60", token=token,
                ).status_code)
            self.assertEqual(429, self.login_post(
                client, email="spray-next@example.com",
                ip="198.51.100.60", token=token,
            ).status_code)

        self.execute("TRUNCATE TABLE rate_limit_counters")
        app, limiter = self.make_login_app(policy)
        with patch("app.routes.auth._get_login_limiter", return_value=limiter), patch(
            "app.routes.auth.verify_recaptcha", return_value=SimpleNamespace(success=True, reason="")
        ), patch("app.routes.auth.get_connection", return_value=_AuthConnection()), patch(
            "app.routes.auth.check_password_hash", return_value=False
        ):
            for number in range(3):
                separate_client = app.test_client()
                separate_token = self.login_token(separate_client)
                self.assertEqual(401, self.login_post(
                    separate_client, email="victim@example.com",
                    ip=f"203.0.113.{number + 1}", token=separate_token,
                ).status_code)
            fourth_client = app.test_client()
            fourth_token = self.login_token(fourth_client)
            self.assertEqual(429, self.login_post(
                fourth_client, email="victim@example.com",
                ip="203.0.113.99", token=fourth_token,
            ).status_code)

    def test_real_mysql_success_reset_retains_ip_state(self):
        policy = LoginLimiterPolicy(
            ip_threshold=20, ip_window_seconds=900, ip_block_seconds=900,
            account_threshold=15, account_window_seconds=900,
            account_block_seconds=900, ip_account_threshold=5,
            ip_account_window_seconds=900, ip_account_block_seconds=900,
        )
        app, limiter = self.make_login_app(policy)
        limiter.record_failure("owner@example.com", "198.51.100.70")
        user = {
            "id": 7, "name": "Fixture Owner", "role": "owner",
            "password_hash": generate_password_hash("correct-password"),
        }
        client = app.test_client()
        token = self.login_token(client)
        with patch("app.routes.auth._get_login_limiter", return_value=limiter), patch(
            "app.routes.auth.verify_recaptcha", return_value=SimpleNamespace(success=True, reason="")
        ), patch("app.routes.auth.get_connection", return_value=_AuthConnection(user)), patch(
            "app.routes.auth.has_active_subscription", return_value=False
        ):
            response = self.login_post(
                client, email="owner@example.com", ip="198.51.100.70",
                token=token, password="correct-password",
            )
        self.assertEqual(302, response.status_code)
        self.assertEqual(1, limiter.check_ip("198.51.100.70").attempt_count)
        account, pair = limiter.check_account_and_pair(
            "owner@example.com", "198.51.100.70"
        )
        self.assertEqual(0, account.attempt_count)
        self.assertEqual(0, pair.attempt_count)


if __name__ == "__main__":
    unittest.main()
