import unittest

from flask import Flask, render_template_string

from app.services.ses_email_service import render_email
from app.utils.plan_display_utils import display_plan_name, register_plan_display_filter


class PlanDisplayUtilsTests(unittest.TestCase):
    def test_starter_variants_display_as_premium(self):
        for value in ("starter", "Starter", "STARTER", " starter "):
            self.assertEqual("Premium", display_plan_name(value))

    def test_other_and_internal_values_are_unchanged(self):
        self.assertEqual("enterprise", display_plan_name("enterprise"))
        self.assertIsNone(display_plan_name(None))
        stored_plan = "starter"
        display = display_plan_name(stored_plan)
        self.assertEqual("starter", stored_plan)
        self.assertEqual("Premium", display)

    def test_jinja_filter_displays_premium(self):
        app = Flask(__name__)
        register_plan_display_filter(app)
        with app.app_context():
            self.assertEqual("Premium", render_template_string(
                "{{ plan | display_plan_name }}", plan="starter"
            ))

    def test_subscription_email_displays_premium_not_internal_identifier(self):
        data = {
            "customer_name": "Asha", "plan_name": "starter",
            "amount_display": "₹1,499.00", "currency": "INR",
            "razorpay_payment_id": "pay_test", "razorpay_order_id": "order_test",
            "subscription_start_date": "02 Aug 2026", "subscription_end_date": "01 Sep 2026",
            "dashboard_url": "https://reviewgrow.in/my-businesses",
            "support_email": "founder@reviewgrow.in",
        }
        html, text = render_email("subscription_confirmation", data)
        self.assertIn("Premium", html); self.assertIn("Premium", text)
        self.assertNotIn(">starter<", html); self.assertNotIn("Plan: starter", text)


if __name__ == "__main__":
    unittest.main()
