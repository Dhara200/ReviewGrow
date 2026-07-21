import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from app.routes.subscription import subscription_bp
from app.services.csrf_service import get_csrf_token, init_csrf
from app.services.razorpay_service import PaymentError, handle_webhook, resolve_plan
from app.services.subscription_service import activate_or_extend_subscription


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


class RazorpayRouteTests(unittest.TestCase):
    def setUp(self):
        template_dir = Path(__file__).resolve().parents[1] / "app" / "templates"
        static_dir = Path(__file__).resolve().parents[1] / "app" / "static"
        self.app = Flask(
            __name__, template_folder=str(template_dir), static_folder=str(static_dir)
        )
        self.app.config.update(
            TESTING=True, SECRET_KEY="test", RAZORPAY_KEY_ID="rzp_test_public",
            ORIGINAL_SUBSCRIPTION_PRICE=2999,
        )
        init_csrf(self.app)
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

    def test_templates_do_not_expose_secrets_or_razorpay_admin_actions(self):
        root = Path(__file__).resolve().parents[1]
        pricing = (root / "app/templates/pricing.html").read_text(encoding="utf-8")
        admin = (root / "app/templates/admin_payments.html").read_text(encoding="utf-8")
        self.assertNotIn("RAZORPAY_KEY_SECRET", pricing)
        self.assertNotIn("RAZORPAY_WEBHOOK_SECRET", pricing)
        self.assertIn('payment.payment_gateway != "razorpay"', admin)


if __name__ == "__main__":
    unittest.main()
