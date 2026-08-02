import logging
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from app.services import razorpay_service
from app.services.email_queue_service import (
    SUBSCRIPTION_CONFIRMATION_SUBJECT,
    enqueue_subscription_confirmation_email,
)
from app.services.razorpay_service import (
    PaymentError, ensure_subscription_confirmation_email, process_success,
)
from app.services.ses_email_service import render_email
from tests.test_razorpay_payments import FakeProvider


def paid_provider():
    return FakeProvider(
        {"id": "pay_confirm", "order_id": "order_confirm", "amount": 149900,
         "currency": "INR", "status": "captured"},
        {"id": "order_confirm", "amount": 149900, "amount_paid": 149900,
         "currency": "INR", "status": "paid"},
    )


class ProcessCursor:
    def __init__(self, payment):
        self.payment = payment
        self.rowcount = 1
        self.executions = []
    def execute(self, sql, params): self.executions.append((sql, params))
    def fetchone(self): return self.payment
    def close(self): pass


class ProcessConnection:
    def __init__(self, payment):
        self.cursor_value = ProcessCursor(payment)
        self.committed = False
        self.rolled_back = False
        self.closed = False
    def cursor(self, dictionary=False): return self.cursor_value
    def commit(self): self.committed = True
    def rollback(self): self.rolled_back = True
    def close(self): self.closed = True


class LookupCursor:
    def __init__(self, row): self.row = row
    def execute(self, sql, params): self.query, self.params = sql, params
    def fetchone(self): return self.row
    def close(self): pass


class LookupConnection:
    def __init__(self, row): self.cursor_value = LookupCursor(row)
    def cursor(self, dictionary=False): return self.cursor_value
    def close(self): pass


class SubscriptionConfirmationEmailTests(unittest.TestCase):
    def payment(self, processed=False):
        return {
            "id": 11, "user_id": 7, "payment_gateway": "razorpay",
            "razorpay_order_id": "order_confirm",
            "razorpay_payment_id": "pay_confirm" if processed else None,
            "processed_at": object() if processed else None,
            "plan_code": "starter_monthly", "amount_paise": 149900,
            "currency": "INR",
        }

    @patch("app.services.razorpay_service._queue_confirmation_after_commit")
    @patch("app.services.razorpay_service.activate_or_extend_subscription")
    @patch("app.services.razorpay_service.get_connection")
    def test_activation_commits_before_confirmation_queue(
        self, get_connection, activate, queue_confirmation
    ):
        connection = ProcessConnection(self.payment())
        get_connection.return_value = connection
        activate.return_value = (5, datetime.now(timezone.utc))
        queue_confirmation.side_effect = lambda _payment_id: self.assertTrue(
            connection.committed
        )
        result = process_success(
            "order_confirm", "pay_confirm", user_id=7, client=paid_provider()
        )
        self.assertEqual((True, False), result)
        activate.assert_called_once()
        queue_confirmation.assert_called_once_with("pay_confirm")
        self.assertFalse(connection.rolled_back)

    @patch("app.services.razorpay_service._queue_confirmation_after_commit")
    @patch("app.services.razorpay_service.activate_or_extend_subscription",
           side_effect=RuntimeError("subscription update failed"))
    @patch("app.services.razorpay_service.get_connection")
    def test_subscription_failure_rolls_back_and_never_queues(
        self, get_connection, _activate, queue_confirmation
    ):
        connection = ProcessConnection(self.payment())
        get_connection.return_value = connection
        with self.assertRaises(RuntimeError):
            process_success(
                "order_confirm", "pay_confirm", user_id=7, client=paid_provider()
            )
        self.assertTrue(connection.rolled_back)
        queue_confirmation.assert_not_called()

    @patch("app.services.razorpay_service._queue_confirmation_after_commit")
    @patch("app.services.razorpay_service.get_connection")
    def test_invalid_signature_never_activates_or_queues(
        self, get_connection, queue_confirmation
    ):
        connection = ProcessConnection(self.payment())
        get_connection.return_value = connection
        with patch("app.services.razorpay_service.activate_or_extend_subscription") as activate:
            with self.assertRaisesRegex(PaymentError, "signature verification failed"):
                process_success(
                    "order_confirm", "pay_confirm", user_id=7,
                    signature="invalid", client=paid_provider(),
                )
        self.assertTrue(connection.rolled_back)
        activate.assert_not_called()
        queue_confirmation.assert_not_called()

    @patch("app.services.razorpay_service._queue_confirmation_after_commit")
    @patch("app.services.razorpay_service.get_connection")
    def test_uncaptured_payment_never_activates_or_queues(
        self, get_connection, queue_confirmation
    ):
        connection = ProcessConnection(self.payment())
        get_connection.return_value = connection
        provider = paid_provider()
        provider.payment.entity["status"] = "authorized"
        provider.order.entity.update(status="created", amount_paid=0)
        with patch("app.services.razorpay_service.activate_or_extend_subscription") as activate:
            with self.assertRaisesRegex(PaymentError, "not been captured"):
                process_success(
                    "order_confirm", "pay_confirm", user_id=7, client=provider
                )
        activate.assert_not_called()
        queue_confirmation.assert_not_called()

    @patch("app.services.razorpay_service.ensure_subscription_confirmation_email",
           side_effect=RuntimeError("queue unavailable"))
    def test_queue_failure_is_safe_after_committed_payment(self, _ensure):
        with self.assertLogs(razorpay_service.logger, logging.ERROR) as logs:
            razorpay_service._queue_confirmation_after_commit("pay_confirm")
        rendered = " ".join(logs.output)
        self.assertIn("payment_id=pay_confirm", rendered)
        self.assertIn("error_type=RuntimeError", rendered)
        self.assertNotIn("queue unavailable", rendered)

    @patch("app.services.email_queue_service.enqueue_email")
    def test_queue_helper_builds_authoritative_deduplicated_payload(self, enqueue):
        enqueue.return_value = 88
        start = datetime(2026, 8, 1, 9, 29)
        end = datetime(2026, 8, 31, 9, 29)
        job_id = enqueue_subscription_confirmation_email({
            "user_id": 7, "email": "owner@example.com", "name": "Asha",
            "plan_name": "ReviewGrow Premium", "amount_paise": 149900,
            "currency": "INR", "razorpay_payment_id": "pay_confirm",
            "razorpay_order_id": "order_confirm",
            "subscription_start_date": start, "subscription_end_date": end,
        })
        self.assertEqual(88, job_id)
        args, kwargs = enqueue.call_args
        self.assertEqual("owner@example.com", args[0])
        self.assertEqual("subscription_confirmation", args[1])
        self.assertEqual("subscription_confirmation", args[2])
        payload = args[3]
        self.assertEqual(SUBSCRIPTION_CONFIRMATION_SUBJECT, payload["subject"])
        self.assertEqual("₹1,499.00", payload["amount_display"])
        self.assertEqual("pay_confirm", payload["razorpay_payment_id"])
        self.assertIn("IST", payload["subscription_end_date"])
        self.assertEqual(7, kwargs["user_id"])
        self.assertEqual(20, kwargs["priority"])
        self.assertEqual(
            "subscription_confirmation:pay_confirm", kwargs["deduplication_key"]
        )

    @patch("app.services.razorpay_service.enqueue_subscription_confirmation_email")
    @patch("app.services.razorpay_service.get_connection")
    def test_recovery_queues_processed_payment_without_subscription_mutation(
        self, get_connection, enqueue
    ):
        get_connection.return_value = LookupConnection({
            "user_id": 7, "name": "Asha", "email": "owner@example.com",
            "amount_paise": 149900, "currency": "INR",
            "plan_code": "starter_monthly", "razorpay_payment_id": "pay_confirm",
            "razorpay_order_id": "order_confirm",
            "subscription_start_date": datetime(2026, 8, 1, 9, 29),
            "subscription_end_date": datetime(2026, 8, 31, 9, 29),
        })
        enqueue.return_value = 88
        self.assertEqual(88, ensure_subscription_confirmation_email("pay_confirm"))
        enqueue.assert_called_once()
        self.assertEqual("ReviewGrow Premium", enqueue.call_args.args[0]["plan_name"])

    @patch("app.services.razorpay_service.get_connection")
    def test_recovery_feature_flag_skips_database_and_email(self, get_connection):
        with patch.object(
            razorpay_service.Config, "SUBSCRIPTION_CONFIRMATION_EMAIL_ENABLED", False
        ):
            self.assertIsNone(ensure_subscription_confirmation_email("pay_confirm"))
        get_connection.assert_not_called()

    def test_confirmation_templates_render_without_secrets(self):
        context = {
            "customer_name": "Asha", "plan_name": "ReviewGrow Premium",
            "amount_display": "₹1,499.00", "currency": "INR",
            "razorpay_payment_id": "pay_confirm", "razorpay_order_id": "order_confirm",
            "subscription_start_date": "01 Aug 2026, 02:59 PM IST",
            "subscription_end_date": "31 Aug 2026, 02:59 PM IST",
            "dashboard_url": "https://reviewgrow.in/my-businesses",
            "support_email": "founder@reviewgrow.in",
        }
        html, text = render_email("subscription_confirmation", context)
        for expected in ("Asha", "ReviewGrow Premium", "₹1,499.00", "pay_confirm"):
            self.assertIn(expected, html)
            self.assertIn(expected, text)
        combined = html + text
        self.assertNotIn("signature", combined.lower())
        self.assertNotIn("RAZORPAY_KEY_SECRET", combined)


if __name__ == "__main__":
    unittest.main()
