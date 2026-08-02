import unittest

from app.services.ses_email_service import render_email


class RenewalReminderEmailTests(unittest.TestCase):
    def test_html_and_text_render_expected_safe_content(self):
        data = {
            "customer_name": "Asha", "plan_name": "ReviewGrow Premium",
            "subscription_end_date": "07 Aug 2026, 03:30 PM IST",
            "days_remaining": 5, "renewal_url": "https://reviewgrow.in/pricing",
            "support_email": "founder@reviewgrow.in",
            "expected_subscription_end": "2026-08-07T10:00:00",
            "expected_subscription_end_ist": "2026-08-07",
            "subject": "Your ReviewGrow subscription expires in 5 days",
        }
        html, text = render_email("renewal_reminder", data)
        for content in (html, text):
            self.assertIn("07 Aug 2026", content)
            self.assertIn("https://reviewgrow.in/pricing", content)
            self.assertIn("founder@reviewgrow.in", content)
            self.assertNotIn("otp_code", content)


if __name__ == "__main__":
    unittest.main()
