import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from app.routes.subscription import subscription_bp
from app.services.csrf_service import get_csrf_token, init_csrf
from app.services.razorpay_service import (
    PaymentError, _provider_is_paid, create_order, handle_webhook, process_success,
    resolve_plan,
)
from app.services.subscription_service import activate_or_extend_subscription
from app.utils.plan_display_utils import register_plan_display_filter


class FakeSubscriptionCursor:
    def __init__(self, subscription=None):
        self.subscription = subscription
        self.executions = []
        self.lastrowid = 99

    def execute(self, sql, params):
        self.executions.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.subscription


class FakeUserCursor:
    def execute(self, sql, params):
        pass

    def fetchone(self):
        return {"name": "Test Owner", "email": "owner@example.com"}

    def close(self):
        pass


class FakeUserConnection:
    def cursor(self, dictionary=False):
        return FakeUserCursor()

    def close(self):
        pass


class FakePaymentCursor:
    def __init__(self, payment):
        self.payment = payment
        self.executions = []
        self.lastrowid = 11

    def execute(self, sql, params):
        self.executions.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.payment

    def close(self):
        pass


class FakePaymentConnection:
    def __init__(self, payment):
        self.active_cursor = FakePaymentCursor(payment)
        self.committed = False
        self.rolled_back = False

    def cursor(self, dictionary=False):
        return self.active_cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


class FakeProvider:
    class Payment:
        def __init__(self, entity):
            self.entity = entity

        def fetch(self, payment_id):
            return self.entity

    class Order:
        def __init__(self, entity):
            self.entity = entity
            self.created_payload = None

        def fetch(self, order_id):
            return self.entity

        def create(self, payload):
            self.created_payload = payload
            return {
                "id": "order_test",
                "amount": payload["amount"],
                "currency": payload["currency"],
            }

    class Utility:
        def verify_webhook_signature(self, body, signature, secret):
            return None

    def __init__(self, payment, order):
        self.payment = self.Payment(payment)
        self.order = self.Order(order)
        self.utility = self.Utility()


class RazorpayRouteTests(unittest.TestCase):
    def setUp(self):
        template_dir = Path(__file__).resolve().parents[1] / "app" / "templates"
        static_dir = Path(__file__).resolve().parents[1] / "app" / "static"
        self.app = Flask(
            __name__, template_folder=str(template_dir), static_folder=str(static_dir)
        )
        self.app.config.update(
            TESTING=True, SECRET_KEY="test", RAZORPAY_KEY_ID="rzp_test_public",
            ORIGINAL_SUBSCRIPTION_PRICE=1999,
        )
        init_csrf(self.app)
        register_plan_display_filter(self.app)
        self.app.register_blueprint(subscription_bp)

        @self.app.get("/test-token")
        def token():
            return {"token": get_csrf_token()}

        self.client = self.app.test_client()

    def login(self, user_id=7):
        with self.client.session_transaction() as active_session:
            active_session["user_id"] = user_id
        return self.client.get("/test-token").get_json()["token"]

    def test_unauthenticated_user_cannot_create_order(self):
        response = self.client.post("/payments/razorpay/create-order", json={"plan_code": "starter_monthly"})
        self.assertEqual(401, response.status_code)

    def test_browser_amount_is_rejected(self):
        token = self.login()
        response = self.client.post(
            "/payments/razorpay/create-order",
            json={"plan_code": "starter_monthly", "amount": 1},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(400, response.status_code)

    def test_premium_monthly_plan_has_authoritative_price_and_terms(self):
        plan = resolve_plan("starter_monthly")
        self.assertEqual(149900, plan.amount_paise)
        self.assertEqual("INR", plan.currency)
        self.assertEqual(30, plan.duration_days)
        self.assertEqual("ReviewGrow Premium", plan.name)

    @patch("app.services.razorpay_service.get_connection")
    def test_razorpay_order_is_created_for_authoritative_amount(self, connection):
        payment_connection = FakePaymentConnection(None)
        connection.return_value = payment_connection
        provider = FakeProvider({}, {})
        plan, order_id = create_order(7, "starter_monthly", client=provider)
        self.assertEqual("order_test", order_id)
        self.assertEqual(149900, plan.amount_paise)
        self.assertEqual(149900, provider.order.created_payload["amount"])
        self.assertEqual("INR", provider.order.created_payload["currency"])
        self.assertNotIn("amount", provider.order.created_payload["notes"])
        self.assertTrue(payment_connection.committed)

    def test_provider_payment_requires_exact_amount_and_inr_order(self):
        provider = FakeProvider(
            {
                "id": "pay_test", "order_id": "order_test",
                "amount": 149900, "currency": "INR", "status": "captured",
            },
            {
                "id": "order_test", "amount": 149900,
                "amount_paid": 149900, "currency": "INR", "status": "paid",
            },
        )
        self.assertTrue(
            _provider_is_paid(provider, "order_test", "pay_test", 149900, "INR")
        )
        provider.order.entity["amount"] = 199900
        with self.assertRaisesRegex(PaymentError, "Order amount or currency mismatch"):
            _provider_is_paid(provider, "order_test", "pay_test", 149900, "INR")

    @patch("app.services.razorpay_service.get_connection")
    def test_callback_rejects_stored_order_with_wrong_amount(self, connection):
        payment_connection = FakePaymentConnection({
            "id": 11, "user_id": 7, "payment_gateway": "razorpay",
            "razorpay_order_id": "order_test", "processed_at": None,
            "plan_code": "starter_monthly", "amount_paise": 199900,
            "currency": "INR",
        })
        connection.return_value = payment_connection
        with self.assertRaisesRegex(PaymentError, "Stored payment amount"):
            process_success(
                "order_test", "pay_test", user_id=7,
                client=FakeProvider({}, {}),
            )
        self.assertTrue(payment_connection.rolled_back)

    @patch("app.services.razorpay_service.activate_or_extend_subscription")
    @patch("app.services.razorpay_service._queue_confirmation_after_commit")
    @patch("app.services.razorpay_service.get_connection")
    def test_duplicate_callback_does_not_extend_subscription(
        self, connection, queue_confirmation, activate
    ):
        payment_connection = FakePaymentConnection({
            "id": 11, "user_id": 7, "payment_gateway": "razorpay",
            "razorpay_order_id": "order_test", "processed_at": object(),
            "razorpay_payment_id": "pay_test",
            "plan_code": "starter_monthly", "amount_paise": 149900,
            "currency": "INR",
        })
        connection.return_value = payment_connection
        success, duplicate = process_success(
            "order_test", "pay_test", user_id=7,
            client=FakeProvider(
                {"id": "pay_test", "order_id": "order_test", "amount": 149900,
                 "currency": "INR", "status": "captured"},
                {"id": "order_test", "amount": 149900, "amount_paid": 149900,
                 "currency": "INR", "status": "paid"},
            ),
        )
        self.assertTrue(success)
        self.assertTrue(duplicate)
        activate.assert_not_called()
        queue_confirmation.assert_called_once_with("pay_test")

    @patch("app.services.razorpay_service.Config.RAZORPAY_WEBHOOK_SECRET", "test-secret")
    @patch(
        "app.services.razorpay_service.process_success",
        side_effect=PaymentError("Stored payment amount or currency mismatch."),
    )
    def test_webhook_wrong_amount_does_not_activate(self, process):
        payload = (
            b'{"event":"payment.captured","payload":{"payment":{"entity":'
            b'{"id":"pay_test","order_id":"order_test"}}}}'
        )
        with self.assertRaisesRegex(PaymentError, "Stored payment amount"):
            handle_webhook(payload, "valid", client=FakeProvider({}, {}))
        process.assert_called_once_with(
            "order_test", "pay_test", client=process.call_args.kwargs["client"]
        )

    @patch("app.services.razorpay_service.Config.RAZORPAY_WEBHOOK_SECRET", "test-secret")
    @patch("app.services.razorpay_service.process_success", return_value=(True, True))
    def test_duplicate_webhook_does_not_process_again(self, process):
        payload = (
            b'{"event":"order.paid","payload":{"order":{"entity":'
            b'{"id":"order_test","payment_id":"pay_test"}}}}'
        )
        result = handle_webhook(payload, "valid", client=FakeProvider({}, {}))
        self.assertEqual("duplicate", result)
        process.assert_called_once()

    @patch("app.routes.subscription.create_order")
    def test_create_order_returns_server_amount_and_public_key_only(self, mocked):
        plan = resolve_plan("starter_monthly")
        mocked.return_value = (plan, "order_test")
        token = self.login()
        response = self.client.post(
            "/payments/razorpay/create-order", json={"plan_code": "starter_monthly"},
            headers={"X-CSRF-Token": token},
        )
        body = response.get_json()
        self.assertEqual(plan.amount_paise, body["amount"])
        self.assertEqual("rzp_test_public", body["key_id"])
        self.assertEqual("ReviewGrow Premium", body["description"])
        self.assertNotIn("ReviewGrow Starter", response.get_data(as_text=True))
        self.assertNotIn("key_secret", body)

    @patch("app.routes.subscription.get_connection", return_value=FakeUserConnection())
    @patch("app.routes.subscription.has_active_subscription", return_value=False)
    @patch("app.routes.subscription.latest_subscription", return_value=None)
    def test_pricing_and_checkout_display_premium_plan(self, latest, active, connection):
        self.login()
        response = self.client.get("/pricing")
        page = response.get_data(as_text=True)
        self.assertEqual(200, response.status_code)
        self.assertIn("ReviewGrow Premium", page)
        self.assertIn("Purchase ReviewGrow Premium", page)
        self.assertIn("<span class=\"text-muted\">Plan</span>", page)
        self.assertIn("<strong>ReviewGrow Premium</strong>", page)
        self.assertIn('<span class="rs-marketing-price-original">₹1,999</span>', page)
        self.assertIn('<strong>₹1,499</strong>', page)
        self.assertIn("Pay ₹1,499 securely with Razorpay", page)
        self.assertNotIn("ReviewGrow Starter", page)

    @patch("app.routes.subscription.verify_checkout", return_value=(True, False))
    def test_verified_checkout_returns_success(self, mocked):
        token = self.login()
        payload = {"razorpay_order_id": "o", "razorpay_payment_id": "p", "razorpay_signature": "s"}
        response = self.client.post(
            "/payments/razorpay/verify", json=payload,
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.get_json()["success"])

    @patch("app.routes.subscription.verify_checkout", side_effect=PaymentError("Payment signature verification failed."))
    def test_invalid_signature_is_safe(self, mocked):
        token = self.login()
        response = self.client.post(
            "/payments/razorpay/verify",
            json={"razorpay_order_id": "o", "razorpay_payment_id": "p", "razorpay_signature": "bad"},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(400, response.status_code)
        self.assertNotIn("Traceback", response.get_data(as_text=True))

    @patch("app.routes.subscription.handle_webhook", return_value="processed")
    def test_valid_webhook_route_accepts_provider_request(self, mocked):
        response = self.client.post(
            "/webhooks/razorpay", data=b'{}', headers={"X-Razorpay-Signature": "valid"}
        )
        self.assertEqual(200, response.status_code)
        mocked.assert_called_once_with(b'{}', "valid")

    @patch("app.routes.subscription.handle_webhook", side_effect=PaymentError("Invalid webhook signature.", 401))
    def test_invalid_webhook_signature_is_rejected(self, mocked):
        self.assertEqual(401, self.client.post("/webhooks/razorpay", data=b'{}').status_code)

    def test_invalid_plan_is_rejected(self):
        with self.assertRaises(PaymentError):
            resolve_plan("attacker-plan")

    @patch("app.services.razorpay_service.Config.RAZORPAY_WEBHOOK_SECRET", "")
    def test_missing_webhook_secret_fails_closed(self):
        with self.assertRaises(PaymentError) as raised:
            handle_webhook(b"{}", "signature")
        self.assertEqual(503, raised.exception.status_code)

    def test_active_subscription_is_extended_from_existing_expiry(self):
        from datetime import datetime, timedelta
        existing_end = datetime.utcnow() + timedelta(days=10)
        cursor = FakeSubscriptionCursor({
            "id": 5, "subscription_start_date": datetime.utcnow(),
            "subscription_end_date": existing_end,
        })
        subscription_id, new_end = activate_or_extend_subscription(cursor, 7, duration_days=30)
        self.assertEqual(5, subscription_id)
        self.assertEqual(existing_end + timedelta(days=30), new_end)

    def test_missing_subscription_is_created(self):
        cursor = FakeSubscriptionCursor()
        subscription_id, _ = activate_or_extend_subscription(cursor, 7, duration_days=30)
        self.assertEqual(99, subscription_id)
        self.assertIn("INSERT INTO subscriptions", cursor.executions[-1][0])

    def test_expired_subscription_restarts_from_now(self):
        from datetime import datetime, timedelta
        expired_end = datetime.utcnow() - timedelta(days=2)
        cursor = FakeSubscriptionCursor({
            "id": 5, "subscription_start_date": datetime.utcnow() - timedelta(days=32),
            "subscription_end_date": expired_end,
        })
        before = datetime.utcnow()
        subscription_id, new_end = activate_or_extend_subscription(cursor, 7, duration_days=30)
        after = datetime.utcnow()
        self.assertEqual(5, subscription_id)
        self.assertGreaterEqual(new_end, before + timedelta(days=30))
        self.assertLessEqual(new_end, after + timedelta(days=30))

    def test_templates_do_not_expose_secrets_or_razorpay_admin_actions(self):
        root = Path(__file__).resolve().parents[1]
        pricing = (root / "app/templates/pricing.html").read_text(encoding="utf-8")
        admin = (root / "app/templates/admin_payments.html").read_text(encoding="utf-8")
        self.assertNotIn("RAZORPAY_KEY_SECRET", pricing)
        self.assertNotIn("RAZORPAY_WEBHOOK_SECRET", pricing)
        self.assertIn('payment.payment_gateway != "razorpay"', admin)


if __name__ == "__main__":
    unittest.main()
