import unittest
from unittest.mock import patch

from app.jobs import generate_subscription_renewal_reminders as command


class SubscriptionReminderCommandTests(unittest.TestCase):
    @patch.object(command, "validate_runtime_schema")
    @patch.object(command, "generate_subscription_renewal_reminders")
    def test_dry_run_is_forwarded_and_exits_successfully(self, generate, validate):
        generate.return_value = {
            "target_date": "2026-08-07", "scanned": 2, "eligible": 1,
            "queued": 0, "duplicates": 0, "skipped": 1, "errors": 0,
            "disabled": False, "dry_run": True,
        }
        self.assertEqual(0, command.main(["--dry-run"]))
        validate.assert_called_once_with()
        generate.assert_called_once_with(dry_run=True)

    @patch.object(command, "validate_runtime_schema", side_effect=RuntimeError("db"))
    def test_operational_failure_returns_nonzero(self, _validate):
        self.assertEqual(1, command.main([]))


if __name__ == "__main__":
    unittest.main()
