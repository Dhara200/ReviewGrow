import unittest
from unittest.mock import Mock, patch

from app.services.ses_email_service import render_email, send_email


class SesEmailServiceTests(unittest.TestCase):
    def test_builds_utf8_html_and_text_and_omits_blank_configuration_set(self):
        client = Mock()
        client.send_email.return_value = {"MessageId": "ses-123"}
        with patch("app.services.ses_email_service.Config.SES_ENABLED", True), \
             patch("app.services.ses_email_service.Config.SES_CONFIGURATION_SET", ""):
            result = send_email(
                "owner@example.com", "Welcome", "<p>Hello</p>", "Hello", client=client
            )
        self.assertEqual("ses-123", result)
        payload = client.send_email.call_args.kwargs
        self.assertNotIn("ConfigurationSetName", payload)
        self.assertEqual("UTF-8", payload["Content"]["Simple"]["Body"]["Html"]["Charset"])
        self.assertEqual("Hello", payload["Content"]["Simple"]["Body"]["Text"]["Data"])

    @patch("app.services.ses_email_service._create_ses_client")
    def test_disabled_ses_never_creates_aws_client(self, boto_client):
        with patch("app.services.ses_email_service.Config.SES_ENABLED", False):
            message_id = send_email(
                "owner@example.com", "Welcome", "<p>Hello</p>", "Hello"
            )
        self.assertTrue(message_id.startswith("mock-ses-disabled-"))
        boto_client.assert_not_called()

    def test_welcome_template_has_html_and_plain_text(self):
        html, text = render_email("welcome", {
            "display_name": "Asha",
            "login_url": "https://reviewgrow.in/login-page",
            "support_email": "founder@reviewgrow.in",
        })
        self.assertIn("Asha", html)
        self.assertIn("Log in: https://reviewgrow.in/login-page", text)

    def test_login_otp_template_has_subject_content_without_secret_url(self):
        html, text = render_email("login_otp", {
            "customer_name": "Asha", "otp_code": "012345", "expiry_minutes": 5,
            "support_email": "founder@reviewgrow.in",
        })
        self.assertIn("012345", html)
        self.assertIn("expires in 5 minutes", text)
        self.assertNotIn("?otp", html.lower())


if __name__ == "__main__":
    unittest.main()
