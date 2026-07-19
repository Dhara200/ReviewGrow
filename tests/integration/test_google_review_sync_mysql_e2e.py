"""Real-MySQL end-to-end coverage for asynchronous Google review sync.

Only Google HTTP is mocked. Set TEST_MYSQL_* variables as documented in the
adjacent README. Absence of configuration skips this opt-in module; a
configured but unavailable or unsafe database fails loudly.
"""

import copy
import json
import os
import re
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import mysql.connector
import requests
from flask import Flask

import worker
from app.config import Config
from app.routes.google_business import google_business_bp
from app.services.admin_sync_queue_service import AdminSyncQueueService
from app.services.csrf_service import CSRF_SESSION_KEY, init_csrf
from app.services.google_review_sync_execution_service import run_google_review_sync
from app.services.google_review_sync_job_service import GoogleReviewSyncJobService
from app.services.token_crypto_service import encrypt_token


ROOT = Path(__file__).resolve().parents[2]
TRANSIENT_STATUSES = (429, 500, 502, 503, 504)
NON_TRANSIENT_STATUSES = (400, 401, 403, 404)
BASELINE_MANIFEST = ROOT / "database" / "migration_baseline.json"

AI_BASELINE_COLUMNS = {
    "reviews": {
        "category": ("varchar(100)", "YES"),
        "complaint_praise_theme": ("varchar(255)", "YES"),
        "suggested_reply": ("text", "YES"),
        "confidence_score": ("decimal(5,4)", "YES"),
        "analysis_error": ("text", "YES"),
    },
    "analysis_jobs": {
        "id": ("int", "NO"), "user_id": ("int", "NO"),
        "business_id": ("int", "NO"),
        "status": ("enum('pending','processing','completed','failed')", "YES"),
        "total_reviews": ("int", "YES"), "processed_reviews": ("int", "YES"),
        "failed_reviews": ("int", "YES"), "error_message": ("text", "YES"),
        "force_reanalysis": ("tinyint(1)", "YES"),
        "latest_report_id": ("int", "YES"), "created_at": ("timestamp", "YES"),
        "started_at": ("datetime", "YES"), "completed_at": ("datetime", "YES"),
    },
    "ai_usage_logs": {
        "id": ("int", "NO"), "user_id": ("int", "NO"),
        "business_id": ("int", "YES"), "provider": ("varchar(50)", "NO"),
        "model_name": ("varchar(100)", "NO"),
        "operation_type": ("varchar(100)", "NO"),
        "input_tokens": ("int", "YES"), "output_tokens": ("int", "YES"),
        "total_tokens": ("int", "YES"),
        "estimated_cost": ("decimal(12,6)", "YES"),
        "request_status": ("enum('success','failed')", "YES"),
        "response_time_ms": ("int", "YES"), "error_message": ("text", "YES"),
        "created_at": ("timestamp", "YES"),
    },
    "ai_monthly_usage": {
        "id": ("int", "NO"), "user_id": ("int", "NO"),
        "business_id": ("int", "YES"), "provider": ("varchar(50)", "NO"),
        "model_name": ("varchar(100)", "NO"), "usage_month": ("date", "NO"),
        "total_requests": ("int", "YES"),
        "successful_requests": ("int", "YES"), "failed_requests": ("int", "YES"),
        "total_input_tokens": ("bigint", "YES"),
        "total_output_tokens": ("bigint", "YES"), "total_tokens": ("bigint", "YES"),
        "total_estimated_cost": ("decimal(12,6)", "YES"),
        "average_response_time_ms": ("decimal(12,2)", "YES"),
        "updated_at": ("timestamp", "YES"),
    },
}

AI_BASELINE_INDEXES = {
    "analysis_jobs": {
        "PRIMARY": (True, ("id",)),
        "idx_analysis_jobs_status": (False, ("status",)),
        "idx_analysis_jobs_user_id": (False, ("user_id",)),
        "idx_analysis_jobs_business_id": (False, ("business_id",)),
        "idx_analysis_jobs_created_at": (False, ("created_at",)),
        "idx_analysis_jobs_business_status": (False, ("business_id", "status")),
    },
    "ai_usage_logs": {
        "PRIMARY": (True, ("id",)),
        "idx_ai_usage_user_id": (False, ("user_id",)),
        "idx_ai_usage_business_id": (False, ("business_id",)),
        "idx_ai_usage_created_at": (False, ("created_at",)),
        "idx_ai_usage_provider_model": (False, ("provider", "model_name")),
        "idx_ai_usage_month": (False, ("created_at", "user_id", "business_id")),
    },
    "ai_monthly_usage": {
        "PRIMARY": (True, ("id",)),
        "uniq_ai_monthly_usage": (
            True, ("user_id", "business_id", "provider", "model_name", "usage_month")
        ),
        "idx_ai_monthly_user": (False, ("user_id",)),
        "idx_ai_monthly_business": (False, ("business_id",)),
        "idx_ai_monthly_month": (False, ("usage_month",)),
    },
}

AI_BASELINE_FOREIGN_KEYS = {
    ("analysis_jobs", "user_id", "users", "id", "CASCADE"),
    ("analysis_jobs", "business_id", "businesses", "id", "CASCADE"),
    ("ai_usage_logs", "user_id", "users", "id", "CASCADE"),
    ("ai_usage_logs", "business_id", "businesses", "id", "SET NULL"),
    ("ai_monthly_usage", "user_id", "users", "id", "CASCADE"),
    ("ai_monthly_usage", "business_id", "businesses", "id", "SET NULL"),
}

AI_BASELINE_DEFAULTS = {
    "analysis_jobs": {
        "status": "pending", "total_reviews": "0", "processed_reviews": "0",
        "failed_reviews": "0", "force_reanalysis": "0",
        "created_at": "current_timestamp",
    },
    "ai_usage_logs": {
        "input_tokens": "0", "output_tokens": "0", "total_tokens": "0",
        "estimated_cost": "0", "request_status": "success",
        "response_time_ms": "0", "created_at": "current_timestamp",
    },
    "ai_monthly_usage": {
        "total_requests": "0", "successful_requests": "0", "failed_requests": "0",
        "total_input_tokens": "0", "total_output_tokens": "0", "total_tokens": "0",
        "total_estimated_cost": "0", "average_response_time_ms": "0",
        "updated_at": "current_timestamp",
    },
}


def _settings():
    database = os.getenv("TEST_MYSQL_DATABASE")
    if not database:
        raise unittest.SkipTest(
            "Real MySQL integration tests skipped: set TEST_MYSQL_DATABASE and "
            "the remaining TEST_MYSQL_* variables; see tests/integration/README.md"
        )
    normal_names = {
        value for value in (Config.DB_NAME, os.getenv("MYSQL_DATABASE"), "reputation_db")
        if value
    }
    if not re.fullmatch(r"[A-Za-z0-9_]+", database):
        raise RuntimeError("TEST_MYSQL_DATABASE may contain only letters, numbers, and underscores")
    if not database.lower().endswith("_test"):
        raise RuntimeError("TEST_MYSQL_DATABASE must end in '_test'")
    if database in normal_names:
        raise RuntimeError("TEST_MYSQL_DATABASE must not match an application database")
    return {
        "host": os.getenv("TEST_MYSQL_HOST", "127.0.0.1"),
        "port": int(os.getenv("TEST_MYSQL_PORT", "3306")),
        "user": os.getenv("TEST_MYSQL_USER", "root"),
        "password": os.getenv("TEST_MYSQL_PASSWORD", ""),
        "database": database,
    }


def _connect(settings, database=True):
    options = {key: settings[key] for key in ("host", "port", "user", "password")}
    if database:
        options["database"] = settings["database"]
    return mysql.connector.connect(**options)


def _load_baseline_manifest(path=BASELINE_MANIFEST):
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("baseline") != "database/init.sql":
        raise RuntimeError("Migration baseline must identify database/init.sql")
    superseded = manifest.get("superseded_migrations")
    if not isinstance(superseded, list) or not all(isinstance(name, str) for name in superseded):
        raise RuntimeError("superseded_migrations must be a list of filenames")
    if len(superseded) != len(set(superseded)):
        raise RuntimeError("Migration baseline contains duplicate filenames")
    migration_names = {
        path.name for path in (ROOT / "database" / "migrations").glob("*.sql")
    }
    for name in superseded:
        if Path(name).name != name or "/" in name or "\\" in name:
            raise RuntimeError(f"Unsafe migration baseline filename: {name!r}")
        if name not in migration_names:
            raise RuntimeError(f"Superseded migration does not exist: {name}")
        if name not in manifest.get("reasons", {}):
            raise RuntimeError(f"Superseded migration has no documented reason: {name}")
    return manifest


def _execute_script(connection, path, database):
    script = path.read_text(encoding="utf-8")
    cursor = connection.cursor()
    try:
        for statement in script.split(";"):
            statement = statement.strip()
            if statement:
                executable = re.sub(r"(?m)^\s*--.*$", "", statement).strip()
                if re.fullmatch(
                    r"(?is)CREATE\s+DATABASE\s+IF\s+NOT\s+EXISTS\s+`?\w+`?",
                    executable,
                ) or re.fullmatch(r"(?is)USE\s+`?\w+`?", executable):
                    # Database-selection statements are runner metadata. Pin the
                    # unmodified script's execution to the validated test schema.
                    connection.database = database
                    continue
                cursor.execute(statement)
                if cursor.with_rows:
                    cursor.fetchall()
                while cursor.nextset():
                    if cursor.with_rows:
                        cursor.fetchall()
    finally:
        cursor.close()


def _assert_ai_baseline_schema(connection, database):
    cursor = connection.cursor(dictionary=True)
    try:
        for table, expected_columns in AI_BASELINE_COLUMNS.items():
            cursor.execute(
                "SELECT column_name AS name,column_type AS type,is_nullable AS nullable," 
                "column_default AS default_value,extra AS extra "
                "FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s",
                (database, table),
            )
            actual = {
                row["name"]: (row["type"].lower(), row["nullable"])
                for row in cursor.fetchall()
            }
            for column, definition in expected_columns.items():
                if actual.get(column) != definition:
                    raise AssertionError(
                        f"database/init.sql baseline drift: {table}.{column} "
                        f"expected {definition}, found {actual.get(column)}"
                    )

            cursor.execute(
                "SELECT column_name AS name,column_default AS default_value,extra AS extra "
                "FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s",
                (database, table),
            )
            metadata = {row["name"]: row for row in cursor.fetchall()}
            if table != "reviews" and "auto_increment" not in metadata["id"]["extra"].lower():
                raise AssertionError(
                    f"database/init.sql baseline drift: {table}.id is not AUTO_INCREMENT"
                )
            for column, expected_default in AI_BASELINE_DEFAULTS.get(table, {}).items():
                value = metadata[column]["default_value"]
                normalized = str(value).lower() if value is not None else None
                if normalized and re.fullmatch(r"-?\d+\.0+", normalized):
                    normalized = normalized.split(".", 1)[0]
                if normalized != expected_default:
                    raise AssertionError(
                        f"database/init.sql baseline drift: default {table}.{column} "
                        f"expected {expected_default!r}, found {normalized!r}"
                    )

        for table, expected_indexes in AI_BASELINE_INDEXES.items():
            cursor.execute(
                "SELECT index_name AS name,non_unique AS non_unique," 
                "column_name AS column_name,seq_in_index AS position "
                "FROM information_schema.statistics "
                "WHERE table_schema=%s AND table_name=%s "
                "ORDER BY index_name,seq_in_index",
                (database, table),
            )
            grouped = {}
            for row in cursor.fetchall():
                value = grouped.setdefault(
                    row["name"], [row["non_unique"] == 0, []]
                )
                value[1].append(row["column_name"])
            actual = {name: (value[0], tuple(value[1])) for name, value in grouped.items()}
            for name, definition in expected_indexes.items():
                if actual.get(name) != definition:
                    raise AssertionError(
                        f"database/init.sql baseline drift: index {table}.{name} "
                        f"expected {definition}, found {actual.get(name)}"
                    )

        cursor.execute(
            "SELECT k.table_name AS table_name,k.column_name AS column_name," 
            "k.referenced_table_name AS referenced_table_name," 
            "k.referenced_column_name AS referenced_column_name," 
            "r.delete_rule AS delete_rule "
            "FROM information_schema.key_column_usage k "
            "JOIN information_schema.referential_constraints r "
            "ON r.constraint_schema=k.constraint_schema "
            "AND r.constraint_name=k.constraint_name "
            "WHERE k.constraint_schema=%s AND k.referenced_table_name IS NOT NULL",
            (database,),
        )
        actual_foreign_keys = {
            (
                row["table_name"], row["column_name"], row["referenced_table_name"],
                row["referenced_column_name"], row["delete_rule"],
            )
            for row in cursor.fetchall()
        }
        missing = AI_BASELINE_FOREIGN_KEYS - actual_foreign_keys
        if missing:
            raise AssertionError(
                f"database/init.sql baseline drift: missing foreign keys {sorted(missing)}"
            )
    finally:
        cursor.close()


def _response(status, payload=None):
    response = MagicMock()
    response.status_code = status
    response.ok = 200 <= status < 300
    response.headers = {}
    response.url = "https://mybusiness.googleapis.com/v4/accounts/1/locations/2/reviews"
    response.json.return_value = payload or {}
    response.text = ""
    return response


def _review(review_id, comment="Review text", rating="FIVE"):
    return {
        "reviewId": review_id,
        "starRating": rating,
        "comment": comment,
        "reviewer": {"displayName": "Integration Reviewer"},
        "createTime": "2026-07-01T00:00:00Z",
        "updateTime": "2026-07-02T00:00:00Z",
    }


class GoogleReviewSyncMySQLE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = _settings()
        cls.baseline_manifest = _load_baseline_manifest()
        try:
            admin = _connect(cls.settings, database=False)
        except mysql.connector.Error as error:
            raise RuntimeError(
                "Configured test MySQL service is unavailable; no fallback was used"
            ) from error
        cursor = admin.cursor()
        try:
            cursor.execute(f"DROP DATABASE IF EXISTS `{cls.settings['database']}`")
            cursor.execute(f"CREATE DATABASE `{cls.settings['database']}` CHARACTER SET utf8mb4")
        finally:
            cursor.close()
            admin.close()

        schema = _connect(cls.settings)
        try:
            _execute_script(schema, ROOT / "database" / "init.sql", cls.settings["database"])
            _assert_ai_baseline_schema(schema, cls.settings["database"])
            baseline_cursor = schema.cursor()
            try:
                baseline_cursor.execute(
                    "CREATE TABLE schema_migrations ("
                    "version VARCHAR(255) PRIMARY KEY," 
                    "applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                )
                for migration_name in cls.baseline_manifest["superseded_migrations"]:
                    baseline_cursor.execute(
                        "INSERT INTO schema_migrations(version) VALUES (%s)",
                        (migration_name,),
                    )
            finally:
                baseline_cursor.close()
            superseded = set(cls.baseline_manifest["superseded_migrations"])
            for migration in sorted((ROOT / "database" / "migrations").glob("*.sql")):
                if migration.name in superseded:
                    print(
                        "Skipping baseline-superseded migration: "
                        f"{migration.name} - "
                        f"{cls.baseline_manifest['reasons'][migration.name]}"
                    )
                    continue
                print(f"Applying post-baseline migration: {migration.name}")
                _execute_script(schema, migration, cls.settings["database"])
                ledger_cursor = schema.cursor()
                try:
                    ledger_cursor.execute(
                        "INSERT INTO schema_migrations(version) VALUES (%s)",
                        (migration.name,),
                    )
                finally:
                    ledger_cursor.close()
            schema.commit()
        except Exception:
            schema.rollback()
            schema.close()
            cleanup = _connect(cls.settings, database=False)
            cleanup_cursor = cleanup.cursor()
            try:
                cleanup_cursor.execute(
                    f"DROP DATABASE IF EXISTS `{cls.settings['database']}`"
                )
            finally:
                cleanup_cursor.close()
                cleanup.close()
            raise
        else:
            schema.close()

        cls.original_database_config = (
            Config.DB_HOST, Config.DB_PORT, Config.DB_USER, Config.DB_PASSWORD,
            Config.DB_NAME, Config.SECRET_KEY,
        )
        Config.DB_HOST = cls.settings["host"]
        Config.DB_PORT = cls.settings["port"]
        Config.DB_USER = cls.settings["user"]
        Config.DB_PASSWORD = cls.settings["password"]
        Config.DB_NAME = cls.settings["database"]
        Config.SECRET_KEY = "mysql-integration-secret"

        ledger = _connect(cls.settings)
        ledger_cursor = ledger.cursor()
        try:
            ledger_cursor.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
            cls.baseline_ledger = [row[0] for row in ledger_cursor.fetchall()]
        finally:
            ledger_cursor.close()
            ledger.close()

    @classmethod
    def tearDownClass(cls):
        (
            Config.DB_HOST, Config.DB_PORT, Config.DB_USER, Config.DB_PASSWORD,
            Config.DB_NAME, Config.SECRET_KEY,
        ) = cls.original_database_config
        admin = _connect(cls.settings, database=False)
        cursor = admin.cursor()
        try:
            cursor.execute(f"DROP DATABASE IF EXISTS `{cls.settings['database']}`")
        finally:
            cursor.close()
            admin.close()

    def setUp(self):
        worker.shutdown_requested = False
        worker.shutdown_event.clear()
        connection = self.connection()
        cursor = connection.cursor()
        try:
            cursor.execute("SET FOREIGN_KEY_CHECKS=0")
            cursor.execute("SHOW TABLES")
            tables = [row[0] for row in cursor.fetchall()]
            for table in tables:
                if table == "schema_migrations":
                    continue
                cursor.execute(f"TRUNCATE TABLE `{table}`")
            cursor.execute("SET FOREIGN_KEY_CHECKS=1")
            cursor.execute(
                "INSERT INTO users (id,name,email,password_hash,role) VALUES (7,'Owner','owner@example.test','x','owner')"
            )
            cursor.execute(
                "INSERT INTO businesses (id,user_id,business_name,business_type,city,country) "
                "VALUES (9,7,'Integration Business','Restaurant','Chennai','India')"
            )
            cursor.execute(
                "INSERT INTO subscriptions (user_id,plan_name,status,subscription_start_date,subscription_end_date) "
                "VALUES (7,'test','active',UTC_TIMESTAMP(),DATE_ADD(UTC_TIMESTAMP(),INTERVAL 1 DAY))"
            )
            cursor.execute(
                "INSERT INTO google_business_connections "
                "(id,user_id,business_id,google_account_id,google_account_email," 
                "google_location_id,access_token,refresh_token," 
                "token_expiry,is_connected,connection_status) "
                "VALUES (11,7,9,'accounts/1','owner@example.test','locations/2',%s,%s," 
                "DATE_ADD(UTC_TIMESTAMP(),INTERVAL 1 DAY),TRUE,'connected')",
                (encrypt_token("integration-access-token"), encrypt_token("integration-refresh-token")),
            )
            connection.commit()
        finally:
            cursor.close()
            connection.close()
        self.jobs = GoogleReviewSyncJobService()

    def connection(self):
        return _connect(self.settings)

    def fetchone(self, query, params=()):
        connection = self.connection()
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(query, params)
            return cursor.fetchone()
        finally:
            cursor.close()
            connection.close()

    def fetchall(self, query, params=()):
        connection = self.connection()
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(query, params)
            return cursor.fetchall()
        finally:
            cursor.close()
            connection.close()

    def make_app(self):
        app = Flask(__name__)
        app.config.update(TESTING=True, SECRET_KEY=Config.SECRET_KEY)
        init_csrf(app)
        app.register_blueprint(google_business_bp)
        return app

    def test_real_route_worker_execution_transaction_and_post_sync(self):
        app = self.make_app()
        client = app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = 7
            session["role"] = "owner"
            session[CSRF_SESSION_KEY] = "mysql-csrf-token"

        pages = [
            _response(200, {"reviews": [_review("review-1", "Updated")], "nextPageToken": "two"}),
            _response(200, {"reviews": [_review("review-2", "Inserted", "FOUR")]}),
        ]
        with patch(
            "app.services.google_business_service.requests.get", side_effect=pages
        ) as google_http, patch(
            "app.services.review_topic_service.AIService.generate_json",
            return_value=MagicMock(data={"topics": []}),
        ), patch("worker.claim_next_job", return_value=None):
            enqueue = client.post(
                "/businesses/9/google/review-sync-jobs",
                json={},
                headers={"Accept": "application/json", "X-CSRF-Token": "mysql-csrf-token"},
            )
            self.assertEqual(202, enqueue.status_code)
            job_id = enqueue.get_json()["job_id"]
            self.assertTrue(worker.run_worker_iteration())

        self.assertEqual(2, google_http.call_count)
        job = self.jobs.get_job(job_id, 7)
        self.assertEqual("completed", job["status"])
        self.assertEqual((2, 2, 0), (job["fetched_count"], job["inserted_count"], job["updated_count"]))
        self.assertIsNone(job["worker_id"])
        self.assertIsNone(job["lease_expires_at"])
        self.assertIsNone(job["heartbeat_at"])
        reviews = self.fetchall("SELECT google_review_id,review_text FROM reviews ORDER BY google_review_id")
        self.assertEqual(["review-1", "review-2"], [row["google_review_id"] for row in reviews])
        self.assertIsNotNone(self.fetchone(
            "SELECT last_sync_at FROM google_business_connections WHERE id=11"
        )["last_sync_at"])
        analysis = self.fetchone(
            "SELECT status FROM analysis_jobs WHERE user_id=7 AND business_id=9"
        )
        self.assertIsNotNone(analysis)
        status = client.get(f"/google-review-sync-jobs/{job_id}/status")
        self.assertEqual("completed", status.get_json()["status"])
        self.assertEqual(404, client.get(
            "/businesses/9/google/review-sync-jobs/active"
        ).status_code)

    def test_fresh_install_baseline_ledger_is_authoritative(self):
        self.assertIn("ai_analysis_jobs_migration.sql", self.baseline_ledger)
        self.assertEqual(
            1,
            self.fetchone(
                "SELECT COUNT(*) AS count FROM schema_migrations "
                "WHERE version='ai_analysis_jobs_migration.sql'"
            )["count"],
        )
        self.assertEqual(
            len(list((ROOT / "database" / "migrations").glob("*.sql"))),
            len(self.baseline_ledger),
        )

    def test_data_and_partial_migrations_remain_executable(self):
        connection = self.connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO reviews "
                "(business_id,source,rating,review_title,review_text,source_platform," 
                "google_review_id,google_location_id) "
                "VALUES (9,'google',5,'Google Review','Backfill me','google','backfill-id',NULL)"
            )
            cursor.execute(
                "INSERT INTO reviews "
                "(business_id,source,rating,review_title,review_text,source_platform) "
                "VALUES (9,'excel',4,'Imported','Normalize me','google')"
            )
            connection.commit()
            _execute_script(
                connection,
                ROOT / "database" / "migrations" / "backfill_google_review_locations.sql",
                self.settings["database"],
            )
            _execute_script(
                connection,
                ROOT / "database" / "migrations" / "google_review_replies_migration.sql",
                self.settings["database"],
            )
            connection.commit()
        finally:
            cursor.close()
            connection.close()

        self.assertEqual(
            "locations/2",
            self.fetchone(
                "SELECT google_location_id FROM reviews WHERE google_review_id='backfill-id'"
            )["google_location_id"],
        )
        self.assertEqual(
            "excel",
            self.fetchone(
                "SELECT source_platform FROM reviews WHERE review_text='Normalize me'"
            )["source_platform"],
        )

    def test_worker_topic_provider_failure_uses_keyword_fallback_without_flask(self):
        connection = self.connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO ai_consultant_reports "
                "(business_id,health_status,report_status) "
                "VALUES (9,'healthy','up_to_date')"
            )
            connection.commit()
        finally:
            cursor.close()
            connection.close()

        job_id, created = self.jobs.create_job(7, 9)
        self.assertTrue(created)
        with patch(
            "app.services.google_business_service.requests.get",
            return_value=_response(200, {
                "reviews": [_review("provider-fallback", "Great friendly service")]
            }),
        ), patch(
            "app.services.review_topic_service.AIService.generate_json",
            side_effect=RuntimeError("controlled provider outage"),
        ), patch("worker.claim_next_job", return_value=None):
            self.assertTrue(worker.run_worker_iteration())

        job = self.jobs.get_job(job_id)
        self.assertEqual("completed", job["status"])
        self.assertGreater(
            self.fetchone(
                "SELECT COUNT(*) AS count FROM review_topics WHERE business_id=9"
            )["count"],
            0,
        )
        self.assertEqual(
            "outdated",
            self.fetchone(
                "SELECT report_status FROM ai_consultant_reports WHERE business_id=9"
            )["report_status"],
        )
        self.assertIsNotNone(self.fetchone(
            "SELECT id FROM analysis_jobs WHERE user_id=7 AND business_id=9"
        ))

    def test_execution_transaction_rolls_back_after_review_mutation(self):
        from app.services import google_review_sync_execution_service as execution

        original_sync = execution.sync_google_reviews

        def mutate_then_fail(cursor, connection, allow_internal_api_retry=True):
            original_sync(cursor, connection, allow_internal_api_retry=allow_internal_api_retry)
            raise RuntimeError("controlled post-mutation failure")

        before = self.fetchone(
            "SELECT last_sync_at FROM google_business_connections WHERE id=11"
        )["last_sync_at"]
        with patch("app.services.google_business_service.requests.get", return_value=_response(
            200, {"reviews": [_review("rolled-back-review")]}
        )), patch.object(execution, "sync_google_reviews", side_effect=mutate_then_fail):
            with self.assertRaisesRegex(RuntimeError, "controlled post-mutation failure"):
                run_google_review_sync(7, 9)

        independent = self.fetchone(
            "SELECT COUNT(*) AS count FROM reviews WHERE google_review_id='rolled-back-review'"
        )
        self.assertEqual(0, independent["count"])
        self.assertEqual(before, self.fetchone(
            "SELECT last_sync_at FROM google_business_connections WHERE id=11"
        )["last_sync_at"])
        probe = self.connection()
        probe.ping(reconnect=False)
        probe.close()

    def test_second_page_transient_failure_writes_nothing(self):
        with patch("app.services.google_business_service.requests.get", side_effect=[
            _response(200, {"reviews": [_review("page-one")], "nextPageToken": "two"}),
            _response(503),
        ]):
            with self.assertRaises(Exception):
                run_google_review_sync(7, 9)
        self.assertEqual(0, self.fetchone("SELECT COUNT(*) AS count FROM reviews")["count"])
        self.assertIsNone(self.fetchone(
            "SELECT last_sync_at FROM google_business_connections WHERE id=11"
        )["last_sync_at"])

    def test_concurrent_duplicate_enqueue_uses_unique_constraint(self):
        barrier = threading.Barrier(2)

        def create():
            service = GoogleReviewSyncJobService()
            barrier.wait(timeout=5)
            return service.create_job(7, 9)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: create(), range(2)))
        ids = {result[0] for result in results}
        self.assertEqual(1, len(ids))
        self.assertEqual(1, sum(bool(result[1]) for result in results))
        rows = self.fetchall(
            "SELECT id FROM google_review_sync_jobs WHERE active_business_id=9"
        )
        self.assertEqual(1, len(rows))
        self.assertEqual(ids.pop(), rows[0]["id"])

    def test_atomic_claim_race_and_ownership_guards(self):
        job_id, _ = self.jobs.create_job(7, 9)
        barrier = threading.Barrier(2)

        def claim(owner):
            service = GoogleReviewSyncJobService()
            barrier.wait(timeout=5)
            return owner, service.claim_job(job_id, owner, 4)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, ("worker-a", "worker-b")))
        winners = [owner for owner, claimed in results if claimed]
        self.assertEqual(1, len(winners))
        owner = winners[0]
        other = "worker-b" if owner == "worker-a" else "worker-a"
        row = self.jobs.get_job(job_id)
        self.assertEqual("processing", row["status"])
        self.assertEqual(owner, row["worker_id"])
        self.assertIsNotNone(row["heartbeat_at"])
        self.assertIsNotNone(row["lease_expires_at"])
        old_heartbeat = row["heartbeat_at"]
        old_lease_expiry = row["lease_expires_at"]
        self.assertFalse(self.jobs.heartbeat_job(job_id, other, 4))
        self.assertFalse(
            self.jobs.confirm_and_renew_ownership(job_id, other, 4)
        )
        time.sleep(0.02)
        self.assertTrue(
            self.jobs.confirm_and_renew_ownership(job_id, owner, 4)
        )
        renewed = self.jobs.get_job(job_id)
        self.assertGreater(renewed["heartbeat_at"], old_heartbeat)
        self.assertGreater(renewed["lease_expires_at"], old_lease_expiry)
        result = {"fetched_count": 1, "inserted_count": 1, "updated_count": 0}
        self.assertFalse(self.jobs.complete_job(job_id, other, result))
        self.assertFalse(self.jobs.fail_job(job_id, other, "not owner"))
        self.assertTrue(self.jobs.complete_job(job_id, owner, result))

    def test_database_time_recovery_and_old_owner_rejection(self):
        healthy, _ = self.jobs.create_job(7, 9)
        self.assertTrue(self.jobs.claim_job(healthy, "healthy", 30))
        connection = self.connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO businesses (id,user_id,business_name,business_type) VALUES (10,7,'Expired','Other')"
            )
            cursor.execute(
                "INSERT INTO businesses (id,user_id,business_name,business_type) VALUES (11,7,'Legacy','Other')"
            )
            cursor.execute(
                "INSERT INTO google_review_sync_jobs "
                "(user_id,business_id,active_business_id,status,worker_id,started_at,heartbeat_at,lease_expires_at) "
                "VALUES (7,10,10,'processing','old',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)," 
                "DATE_SUB(UTC_TIMESTAMP(6),INTERVAL 1 SECOND))"
            )
            expired = cursor.lastrowid
            cursor.execute(
                "INSERT INTO google_review_sync_jobs "
                "(user_id,business_id,active_business_id,status,worker_id,started_at,lease_expires_at) "
                "VALUES (7,11,11,'processing','legacy'," 
                "DATE_SUB(UTC_TIMESTAMP(6),INTERVAL 31 MINUTE),NULL)"
            )
            legacy = cursor.lastrowid
            connection.commit()
        finally:
            cursor.close()
            connection.close()

        self.assertEqual(2, self.jobs.recover_expired_processing_jobs(30))
        self.assertEqual("processing", self.jobs.get_job(healthy)["status"])
        self.assertEqual("pending", self.jobs.get_job(expired)["status"])
        self.assertEqual("pending", self.jobs.get_job(legacy)["status"])
        self.assertFalse(self.jobs.complete_job(expired, "old", {
            "fetched_count": 0, "inserted_count": 0, "updated_count": 0
        }))
        self.assertTrue(self.jobs.claim_job(expired, "replacement", 5))
        self.assertFalse(self.jobs.fail_job(expired, "old", "stale owner"))
        self.assertTrue(self.jobs.fail_job(expired, "replacement", "controlled"))

    def test_authoritative_confirmation_blocks_old_worker_after_reassignment(self):
        job_id, _ = self.jobs.create_job(7, 9)
        self.assertTrue(self.jobs.claim_job(job_id, "old-worker", 5))
        connection = self.connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "UPDATE google_review_sync_jobs "
                "SET lease_expires_at=DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 1 SECOND) "
                "WHERE id=%s",
                (job_id,),
            )
            connection.commit()
        finally:
            cursor.close()
            connection.close()

        self.assertEqual(1, self.jobs.recover_expired_processing_jobs(30))
        self.assertTrue(self.jobs.claim_job(job_id, "replacement-worker", 30))
        sync_result = {
            "fetched_count": 1,
            "inserted_count": 1,
            "updated_count": 0,
        }

        with patch("worker.GoogleReviewSyncHeartbeat") as heartbeat_type, \
             patch("worker.run_google_review_sync", return_value=sync_result), \
             patch("worker.perform_google_review_post_sync") as post_sync:
            heartbeat_type.return_value.ownership_lost = False
            self.assertTrue(worker._process_google_review_sync_job(
                {"id": job_id, "user_id": 7, "business_id": 9},
                "old-worker",
            ))
            post_sync.assert_not_called()
            heartbeat_type.return_value.stop.assert_called_once()

        reassigned = self.jobs.get_job(job_id)
        self.assertEqual("processing", reassigned["status"])
        self.assertEqual("replacement-worker", reassigned["worker_id"])

        with patch("worker.GoogleReviewSyncHeartbeat") as heartbeat_type, \
             patch("worker.run_google_review_sync", return_value=sync_result), \
             patch("worker.perform_google_review_post_sync") as post_sync:
            heartbeat_type.return_value.ownership_lost = False
            self.assertTrue(worker._process_google_review_sync_job(
                {"id": job_id, "user_id": 7, "business_id": 9},
                "replacement-worker",
            ))
            post_sync.assert_called_once()

        self.assertEqual("completed", self.jobs.get_job(job_id)["status"])

    def test_heartbeat_renews_during_blocked_google_request_and_cleans_up(self):
        job_id, _ = self.jobs.create_job(7, 9)
        entered = threading.Event()
        release = threading.Event()

        def blocked_request(*_args, **_kwargs):
            entered.set()
            self.assertTrue(release.wait(6))
            return _response(200, {"reviews": [_review("heartbeat-review")]})

        with patch.object(worker.Config, "GOOGLE_REVIEW_SYNC_HEARTBEAT_SECONDS", 1), \
             patch.object(worker.Config, "GOOGLE_REVIEW_SYNC_LEASE_SECONDS", 3), \
             patch("app.services.google_business_service.requests.get", side_effect=blocked_request), \
             patch(
                 "app.services.review_topic_service.AIService.generate_json",
                 return_value=MagicMock(data={"topics": []}),
             ), \
             patch("worker.claim_next_job", return_value=None):
            thread = threading.Thread(target=worker.run_worker_iteration)
            thread.start()
            self.assertTrue(entered.wait(3))
            first = self.jobs.get_job(job_id)
            time.sleep(1.3)
            second = self.jobs.get_job(job_id)
            self.assertGreater(second["heartbeat_at"], first["heartbeat_at"])
            self.assertGreater(second["lease_expires_at"], first["lease_expires_at"])
            self.assertEqual(0, self.jobs.recover_expired_processing_jobs(30))
            release.set()
            thread.join(8)
            self.assertFalse(thread.is_alive())
        final = self.jobs.get_job(job_id)
        self.assertEqual("completed", final["status"])
        self.assertIsNone(final["worker_id"])
        self.assertIsNone(final["heartbeat_at"])
        self.assertIsNone(final["lease_expires_at"])

    def test_production_retry_matrix_has_one_http_call_per_worker_attempt(self):
        original_retries = worker.Config.GOOGLE_REVIEW_SYNC_MAX_RETRIES
        try:
            worker.Config.GOOGLE_REVIEW_SYNC_MAX_RETRIES = 1
            for status in TRANSIENT_STATUSES:
                with self.subTest(status=status):
                    http = MagicMock(side_effect=[_response(status), _response(status)])
                    delays = []
                    with patch("app.services.google_business_service.requests.get", http):
                        with self.assertRaises(Exception):
                            worker._run_google_review_sync_with_retries(
                                {"id": 1, "user_id": 7, "business_id": 9},
                                sleep=delays.append, jitter=lambda _a, _b: 0,
                            )
                    self.assertEqual(2, http.call_count)
                    self.assertEqual(1, len(delays))

            for error in (requests.Timeout("timeout"), requests.ConnectionError("connection")):
                with self.subTest(error=type(error).__name__):
                    http = MagicMock(side_effect=[error, copy.copy(error)])
                    delays = []
                    with patch("app.services.google_business_service.requests.get", http):
                        with self.assertRaises(Exception):
                            worker._run_google_review_sync_with_retries(
                                {"id": 2, "user_id": 7, "business_id": 9},
                                sleep=delays.append, jitter=lambda _a, _b: 0,
                            )
                    self.assertEqual(2, http.call_count)
                    self.assertEqual(1, len(delays))

            for status in NON_TRANSIENT_STATUSES:
                with self.subTest(status=status):
                    http = MagicMock(return_value=_response(status))
                    with patch("app.services.google_business_service.requests.get", http):
                        with self.assertRaises(Exception):
                            worker._run_google_review_sync_with_retries(
                                {"id": 3, "user_id": 7, "business_id": 9},
                                sleep=lambda _delay: self.fail("non-transient error slept"),
                                jitter=lambda _a, _b: 0,
                            )
                    self.assertEqual(1, http.call_count)
        finally:
            worker.Config.GOOGLE_REVIEW_SYNC_MAX_RETRIES = original_retries

    def test_admin_sync_queue_metrics_filters_joins_and_health_use_real_mysql(self):
        connection = self.connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO businesses "
                "(id,user_id,business_name,business_type,city,country) "
                "VALUES (10,7,'Processing Business','Other','Chennai','India')"
            )
            cursor.execute(
                "INSERT INTO google_review_sync_jobs "
                "(user_id,business_id,active_business_id,status,created_at) "
                "VALUES (7,9,9,'pending',DATE_SUB(UTC_TIMESTAMP(),INTERVAL 181 SECOND))"
            )
            cursor.execute(
                "INSERT INTO google_review_sync_jobs "
                "(user_id,business_id,active_business_id,status,worker_id,created_at,"
                "started_at,heartbeat_at,lease_expires_at) "
                "VALUES (7,10,10,'processing','worker-stale',UTC_TIMESTAMP(),"
                "DATE_SUB(UTC_TIMESTAMP(6),INTERVAL 5 MINUTE),"
                "DATE_SUB(UTC_TIMESTAMP(6),INTERVAL 3 MINUTE),"
                "DATE_SUB(UTC_TIMESTAMP(6),INTERVAL 1 SECOND))"
            )
            cursor.execute(
                "INSERT INTO google_review_sync_jobs "
                "(user_id,business_id,status,created_at,started_at,completed_at) "
                "VALUES (7,9,'completed',UTC_TIMESTAMP(),"
                "DATE_SUB(UTC_TIMESTAMP(6),INTERVAL 120 SECOND),"
                "DATE_SUB(UTC_TIMESTAMP(6),INTERVAL 60 SECOND))"
            )
            cursor.execute(
                "INSERT INTO google_review_sync_jobs "
                "(user_id,business_id,status,created_at,started_at,completed_at,error_message) "
                "VALUES (7,9,'failed',UTC_TIMESTAMP(),"
                "DATE_SUB(UTC_TIMESTAMP(6),INTERVAL 30 SECOND),UTC_TIMESTAMP(6),"
                "'Bearer integration-secret-token')"
            )
            connection.commit()
        finally:
            cursor.close()
            connection.close()

        monitor = AdminSyncQueueService()
        summary = monitor.get_sync_queue_summary()
        self.assertEqual(1, summary["pending_jobs"])
        self.assertEqual(1, summary["running_jobs"])
        self.assertEqual(1, summary["completed_today"])
        self.assertEqual(1, summary["failed_today"])
        self.assertAlmostEqual(60, summary["average_sync_seconds"], delta=1)
        self.assertGreaterEqual(summary["oldest_pending_seconds"], 181)

        health = monitor.get_sync_queue_health(summary)
        self.assertEqual(1, health["expired_leases"])
        self.assertEqual(1, health["stale_heartbeats"])
        jobs = monitor.get_recent_sync_jobs("failed", 9, "today")
        self.assertEqual(1, len(jobs))
        self.assertEqual("Integration Business", jobs[0]["business_name"])
        self.assertEqual("Owner", jobs[0]["user_name"])
        self.assertNotIn("integration-secret-token", jobs[0]["safe_error"])
        self.assertNotIn("error_message", jobs[0])


if __name__ == "__main__":
    unittest.main()
