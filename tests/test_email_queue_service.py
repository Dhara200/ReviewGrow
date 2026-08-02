import unittest
from datetime import datetime
from unittest.mock import patch

import mysql.connector

from app.services import email_queue_service as queue


class FakeCursor:
    def __init__(self, rows=None, rowcount=1, duplicate=False):
        self.rows = rows or []
        self.rowcount = rowcount
        self.lastrowid = 31
        self.duplicate = duplicate
        self.executions = []

    def execute(self, sql, params):
        self.executions.append((sql, params))
        if self.duplicate and len(self.executions) == 1:
            raise mysql.connector.IntegrityError(errno=1062, msg="duplicate")

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def close(self): pass


class FakeConnection:
    def __init__(self, cursor): self._cursor = cursor
    def cursor(self, dictionary=False): return self._cursor
    def start_transaction(self): pass
    def commit(self): pass
    def rollback(self): pass
    def close(self): pass


class EmailQueueServiceTests(unittest.TestCase):
    def test_deduplication_returns_existing_job(self):
        cursor = FakeCursor(rows=[(19,)], duplicate=True)
        with patch.object(queue, "get_connection", return_value=FakeConnection(cursor)):
            job_id = queue.enqueue_email(
                "owner@example.com", "welcome", "welcome", {},
                deduplication_key="welcome:7",
            )
        self.assertEqual(19, job_id)
        self.assertIn("deduplication_key=%s", cursor.executions[1][0])

    def test_successful_send_state_stores_message_id_and_timestamp(self):
        cursor = FakeCursor()
        with patch.object(queue, "get_connection", return_value=FakeConnection(cursor)):
            self.assertTrue(queue.mark_email_sent(7, "ses-message"))
        sql, params = cursor.executions[0]
        self.assertIn("sent_at=UTC_TIMESTAMP(6)", sql)
        self.assertEqual(("ses-message", 7), params)

    def test_temporary_failure_schedules_retry(self):
        cursor = FakeCursor()
        with patch.object(queue, "get_connection", return_value=FakeConnection(cursor)):
            self.assertTrue(queue.mark_email_failed(
                {"id": 7, "attempt_count": 1, "max_attempts": 6},
                "temporary", retryable=True,
            ))
        sql, params = cursor.executions[0]
        self.assertIn("status='pending'", sql)
        self.assertIsInstance(params[0], datetime)

    def test_permanent_failure_is_terminal(self):
        cursor = FakeCursor()
        with patch.object(queue, "get_connection", return_value=FakeConnection(cursor)):
            queue.mark_email_failed(
                {"id": 7, "attempt_count": 1, "max_attempts": 6},
                "invalid recipient", retryable=False,
            )
        self.assertIn("status='failed'", cursor.executions[0][0])


if __name__ == "__main__":
    unittest.main()
