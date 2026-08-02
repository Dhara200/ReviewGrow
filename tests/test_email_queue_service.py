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
        self.assertIn("JSON_OBJECT('redacted',TRUE", sql)
        self.assertEqual(("ses-message", 7), params)

    def test_claim_orders_lower_priority_first(self):
        cursor = FakeCursor(rows=[])
        with patch.object(queue, "get_connection", return_value=FakeConnection(cursor)):
            queue.claim_pending_email_jobs()
        self.assertIn("ORDER BY priority ASC, created_at ASC", cursor.executions[0][0])

    def test_renewal_reminder_uses_period_deduplication_and_low_urgency(self):
        cursor = FakeCursor()
        with patch.object(queue, "get_connection", return_value=FakeConnection(cursor)):
            job_id, created = queue.enqueue_renewal_reminder_email({
                "user_id": 7, "email": "owner@example.com",
                "plan_name": "ReviewGrow Premium",
                "subscription_end_date": datetime(2026, 8, 7, 10, 0),
                "subscription_end_date_ist": "2026-08-07",
                "subscription_end_date_display": "07 Aug 2026, 03:30 PM IST",
                "days_remaining": 5,
            })
        self.assertEqual((31, True), (job_id, created))
        params = cursor.executions[0][1]
        self.assertEqual("renewal_reminder", params[2])
        self.assertEqual(100, params[5])
        self.assertEqual("renewal_5_day:7:2026-08-07", params[7])

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
