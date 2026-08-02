import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from app.routes.auth import auth_bp
from app.services.csrf_service import init_csrf


class RegistrationWelcomeEmailTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder="../app/templates")
        self.app.config.update(TESTING=True, SECRET_KEY="welcome-test")
        init_csrf(self.app)
        self.app.register_blueprint(auth_bp)
        self.client = self.app.test_client()

    def payload(self):
        page = self.client.get("/register-page").get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        return {"csrf_token": token, "recaptcha_token": "token", "name": "Asha",
                "email": "asha@example.com", "password": "password1234",
                "confirm_password": "password1234"}

    @patch("app.routes.auth.enqueue_welcome_email")
    @patch("app.routes.auth._create_registered_user", return_value=42)
    @patch("app.routes.auth.verify_recaptcha", return_value=SimpleNamespace(success=True))
    def test_successful_registration_enqueues_welcome(self, _recaptcha, _create, enqueue):
        response = self.client.post("/register-page", data=self.payload())
        self.assertEqual(302, response.status_code)
        enqueue.assert_called_once_with(
            {"id": 42, "name": "Asha", "email": "asha@example.com"}
        )

    @patch("app.routes.auth.enqueue_welcome_email")
    @patch("app.routes.auth._create_registered_user", side_effect=RuntimeError("db failed"))
    @patch("app.routes.auth.verify_recaptcha", return_value=SimpleNamespace(success=True))
    def test_failed_registration_does_not_enqueue(self, _recaptcha, _create, enqueue):
        response = self.client.post("/register-page", data=self.payload())
        self.assertEqual(500, response.status_code)
        enqueue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
