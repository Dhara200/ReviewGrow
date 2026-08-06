import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from app.routes.analysis import analysis_bp
from app.routes.business import business_bp
from app.routes.dashboard import dashboard_bp
from app.routes.google_business import google_business_bp
from app.services.csrf_service import init_csrf


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.executions = []

    def execute(self, query, params=()):
        self.executions.append((" ".join(query.split()), params))

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class FakeConnection:
    def __init__(self, rows):
        self.cursor_value = FakeCursor(rows)

    def cursor(self, dictionary=False):
        return self.cursor_value

    def close(self):
        pass


class MyBusinessesRedesignTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "app"
        app = Flask(
            __name__, template_folder=str(root / "templates"),
            static_folder=str(root / "static"),
        )
        app.config.update(TESTING=True, SECRET_KEY="business-redesign-test")
        init_csrf(app)
        for blueprint in (business_bp, dashboard_bp, analysis_bp, google_business_bp):
            app.register_blueprint(blueprint)
        self.client = app.test_client()

    def login(self):
        with self.client.session_transaction() as session:
            session.update(user_id=7, user_name="Dharaprasath P", role="admin")

    def render(self, rows):
        self.login()
        connection = FakeConnection(rows)
        with patch("app.routes.business.get_connection", return_value=connection):
            response = self.client.get("/my-businesses")
        return response, connection.cursor_value

    def test_empty_state_renders_add_business_cta(self):
        response, cursor = self.render([])
        self.assertEqual(200, response.status_code)
        self.assertIn(b"Add your first business", response.data)
        self.assertIn(b'href="/create-business"', response.data)
        self.assertNotIn(b"deleteBusinessModal", response.data)
        self.assertEqual((7,), cursor.executions[0][1])

    def test_connected_and_disconnected_cards_preserve_actions(self):
        rows = [
            {"id": 11, "business_name": "Dhara Travels", "business_type": "Travel Agency",
             "city": "Udumalpet", "state": "Tiruppur", "country": "India",
             "google_is_connected": True, "google_email": "owner@example.test",
             "google_location_name": "Dhara Travels"},
            {"id": 12, "business_name": "A Very Long Business Name That Must Not Break The Card Layout",
             "business_type": None, "city": None, "state": None, "country": None,
             "google_is_connected": False, "google_email": None,
             "google_location_name": None},
        ]
        response, _cursor = self.render(rows)
        html = response.get_data(as_text=True)
        self.assertEqual(200, response.status_code)
        self.assertIn("2</strong> businesses", html)
        self.assertIn("1</strong> connected to Google", html)
        self.assertIn('/dashboard/11', html)
        self.assertIn('/upload-reviews/11', html)
        self.assertIn('/review-assistant', html)
        self.assertIn('/business/11/live-dashboard', html)
        self.assertIn('/businesses/11/photos', html)
        self.assertIn('/auth/google/start/11?reconnect=1', html)
        self.assertIn('method="POST" action="/businesses/11/google/disconnect"', html)
        self.assertIn('/auth/google/start/12', html)
        self.assertIn('data-business-id="12"', html)
        self.assertIn('id="deleteBusinessForm" method="POST"', html)
        self.assertIn('data-delete-url-template="/business/delete/0"', html)
        self.assertGreaterEqual(html.count('name="csrf_token"'), 2)
        self.assertNotIn("None,", html)

    def test_one_business_uses_constrained_grid(self):
        response, _cursor = self.render([{
            "id": 9, "business_name": "Clinic", "business_type": "Medical Clinic",
            "city": "Chennai", "state": None, "country": "India",
            "google_is_connected": False, "google_email": None, "google_location_name": None,
        }])
        self.assertIn(b"business-grid-single", response.data)
        self.assertIn(b"category-health", response.data)
        self.assertIn(b"Chennai, India", response.data)


if __name__ == "__main__":
    unittest.main()
