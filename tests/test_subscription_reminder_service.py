import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.services import subscription_reminder_service as service


class FakeCursor:
    def __init__(self, rows): self.rows = rows
    def execute(self, sql, params): self.sql, self.params = sql, params
    def fetchall(self): return self.rows
    def fetchone(self): return self.rows[0] if self.rows else None
    def close(self): pass


class FakeConnection:
    def __init__(self, rows): self._cursor = FakeCursor(rows)
    def cursor(self, dictionary=False): return self._cursor
    def close(self): pass


class SubscriptionReminderServiceTests(unittest.TestCase):
    def test_invalid_reminder_day_value_falls_back_safely(self):
        self.assertEqual(5, service._valid_days("invalid"))
        self.assertEqual(5, service._valid_days(31))

    def test_disabled_feature_does_not_query_or_queue(self):
        with patch.object(service.Config, "SUBSCRIPTION_RENEWAL_REMINDER_ENABLED", False), \
             patch.object(service, "get_connection") as connection, \
             patch.object(service, "enqueue_renewal_reminder_email") as enqueue:
            summary = service.generate_subscription_renewal_reminders()
        self.assertTrue(summary["disabled"])
        connection.assert_not_called()
        enqueue.assert_not_called()

    def test_ist_window_crosses_utc_date_boundary(self):
        target, start, end = service.reminder_window_utc(
            datetime(2026, 8, 1, 20, 0, tzinfo=timezone.utc), 5
        )
        self.assertEqual("2026-08-07", target.isoformat())
        self.assertEqual(datetime(2026, 8, 6, 18, 30), start)
        self.assertEqual(datetime(2026, 8, 7, 18, 30), end)

    def test_month_year_and_leap_transitions(self):
        cases = [
            (datetime(2026, 12, 29, tzinfo=timezone.utc), "2027-01-03"),
            (datetime(2028, 2, 24, tzinfo=timezone.utc), "2028-02-29"),
        ]
        for now, expected in cases:
            self.assertEqual(expected, service.reminder_window_utc(now, 5)[0].isoformat())

    @patch.object(service, "enqueue_renewal_reminder_email", return_value=(42, True))
    @patch.object(service, "get_connection")
    def test_eligible_paid_subscription_is_queued_once(self, connection, enqueue):
        end = datetime(2026, 8, 7, 10, 0)
        connection.return_value = FakeConnection([{
            "subscription_id": 1, "user_id": 7, "plan_name": "starter",
            "status": "active", "subscription_end_date": end,
            "name": "Owner", "email": "owner@example.com", "role": "owner",
        }])
        summary = service.generate_subscription_renewal_reminders(
            now=datetime(2026, 8, 2, 0, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(1, summary["queued"])
        details = enqueue.call_args.args[0]
        self.assertEqual("2026-08-07", details["subscription_end_date_ist"])

    @patch.object(service, "get_connection")
    def test_dry_run_does_not_enqueue(self, connection):
        connection.return_value = FakeConnection([])
        with patch.object(service, "enqueue_renewal_reminder_email") as enqueue:
            summary = service.generate_subscription_renewal_reminders(dry_run=True)
        enqueue.assert_not_called()
        self.assertTrue(summary["dry_run"])

    @patch.object(service, "get_connection")
    def test_renewed_subscription_cancels_stale_job(self, connection):
        connection.return_value = FakeConnection([{
            "subscription_end_date": datetime(2026, 9, 1), "status": "active",
            "plan_name": "starter", "name": "Owner",
            "email": "owner@example.com", "role": "owner",
        }])
        eligible, reason, _ = service.validate_renewal_reminder_job({
            "user_id": 7, "recipient_email": "owner@example.com",
            "template_data": {"expected_subscription_end": "2026-08-07T10:00:00"},
        })
        self.assertFalse(eligible)
        self.assertEqual("subscription_renewed", reason)


if __name__ == "__main__":
    unittest.main()
