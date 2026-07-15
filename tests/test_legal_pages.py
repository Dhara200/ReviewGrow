import unittest

from flask import Flask, render_template

from app.routes.auth import auth_bp
from app.routes.legal import legal_bp


class LegalPageTests(unittest.TestCase):
    LEGAL_PAGE_PATHS = (
        "/privacy-policy",
        "/terms-of-service",
        "/data-deletion",
    )

    def setUp(self):
        self.app = Flask(__name__, template_folder="../app/templates")
        self.app.config.update(TESTING=True, SECRET_KEY="test")
        self.app.register_blueprint(auth_bp)
        self.app.register_blueprint(legal_bp)

        @self.app.route("/")
        def homepage():
            return render_template(
                "index.html",
                landing_hero_images=[],
                subscription_price=1999,
                original_subscription_price=2999,
            )

        self.client = self.app.test_client()

    def test_privacy_policy_is_public(self):
        response = self.client.get("/privacy-policy")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"<h1>Privacy Policy</h1>", response.data)

    def test_terms_of_service_is_public(self):
        response = self.client.get("/terms-of-service")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"<h1>Terms of Service</h1>", response.data)

    def test_data_deletion_is_public(self):
        response = self.client.get("/data-deletion")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"<h1>Data Deletion Instructions</h1>", response.data)

    def test_legal_pages_render_shared_contact_details(self):
        for path in self.LEGAL_PAGE_PATHS:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"Dhara Prasath", response.data)
                self.assertIn(b"dharaprasath52@gmail.com", response.data)
                self.assertIn(
                    b'href="mailto:dharaprasath52@gmail.com"',
                    response.data,
                )
                self.assertIn(b"[BUSINESS ADDRESS]", response.data)
                self.assertIn(
                    b'href="https://reviewgrow.in"',
                    response.data,
                )

    def test_legal_pages_do_not_expose_raw_jinja_syntax(self):
        for path in self.LEGAL_PAGE_PATHS:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertNotIn(b"{{ legal.", response.data)
                self.assertNotIn(b"{{", response.data)
                self.assertNotIn(b"}}", response.data)
                self.assertNotIn(b"{%", response.data)
                self.assertNotIn(b"%}", response.data)

    def test_terms_display_seven_day_refund_request_period(self):
        response = self.client.get("/terms-of-service")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"7 days", response.data)

    def test_homepage_footer_links_to_all_legal_pages(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'href="/privacy-policy"', response.data)
        self.assertIn(b'href="/terms-of-service"', response.data)
        self.assertIn(b'href="/data-deletion"', response.data)
        self.assertIn(b'href="/sitemap.xml">Sitemap</a>', response.data)

    def test_registration_links_to_privacy_and_terms(self):
        response = self.client.get("/register-page")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'href="/privacy-policy"', response.data)
        self.assertIn(b'href="/terms-of-service"', response.data)


if __name__ == "__main__":
    unittest.main()
