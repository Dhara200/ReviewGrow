import unittest
from datetime import timedelta
from unittest.mock import patch

from flask import Flask, session

from app.services.session_security_service import (
    init_session_security,
    validate_session_security,
)
from app.routes.google_business import _load_state, _state_for_business


class SessionSecurityTests(unittest.TestCase):
    def make_app(self, environment="production", secure=True, secret="s" * 32):
        app = Flask(__name__, static_folder="../app/static")
        app.config.update(
            TESTING=True,
            APP_ENV=environment,
            SECRET_KEY=secret,
            SESSION_COOKIE_HTTPONLY=True,
            SESSION_COOKIE_SECURE=secure,
            SESSION_COOKIE_SAMESITE="Lax",
            PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
            SESSION_REFRESH_EACH_REQUEST=False,
        )
        init_session_security(app)

        @app.get("/login-test")
        def login_test():
            session.clear()
            session.permanent = True
            session["user_id"] = 7
            return "authenticated"

        @app.get("/private")
        def private():
            return {"authenticated": "user_id" in session}

        return app

    def test_production_cookie_has_secure_httponly_and_lax(self):
        response = self.make_app().test_client().get("/login-test")
        cookie = response.headers["Set-Cookie"]
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_development_explicitly_supports_http_cookie(self):
        app = self.make_app(environment="development", secure=False, secret="dev")
        cookie = app.test_client().get("/login-test").headers["Set-Cookie"]
        self.assertNotIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)

    def test_production_rejects_missing_weak_secret_or_insecure_cookie(self):
        for secret, secure in (("", True), ("short", True), ("s" * 32, False)):
            app = Flask(__name__)
            app.config.update(APP_ENV="production", SECRET_KEY=secret, SESSION_COOKIE_SECURE=secure)
            with self.subTest(secret_length=len(secret), secure=secure):
                with self.assertRaises(RuntimeError):
                    validate_session_security(app)

    def test_lifetime_is_finite_absolute_and_not_refreshed(self):
        app = self.make_app()
        self.assertEqual(timedelta(hours=12), app.permanent_session_lifetime)
        self.assertFalse(app.config["SESSION_REFRESH_EACH_REQUEST"])

    def test_expired_session_no_longer_authenticates(self):
        app = self.make_app()
        serializer = app.session_interface.get_signing_serializer(app)
        with patch("itsdangerous.timed.time.time", return_value=1_000):
            expired_cookie = serializer.dumps({"_permanent": True, "user_id": 7})
        client = app.test_client()
        client.set_cookie(app.config["SESSION_COOKIE_NAME"], expired_cookie)
        with patch("itsdangerous.timed.time.time", return_value=50_000):
            response = client.get("/private")
        self.assertFalse(response.get_json()["authenticated"])

    def test_authenticated_responses_are_not_cached(self):
        response = self.make_app().test_client().get("/login-test")
        self.assertEqual("no-store", response.headers["Cache-Control"])
        self.assertEqual("no-cache", response.headers["Pragma"])

    def test_public_and_static_responses_remain_cacheable(self):
        app = self.make_app()
        client = app.test_client()
        public = client.get("/private")
        self.assertNotIn("Cache-Control", public.headers)
        client.get("/login-test")
        static = client.get("/static/reviewsense.js")
        self.assertNotEqual("no-store", static.headers.get("Cache-Control"))
        static.close()

    @patch("app.routes.google_business.Config.SECRET_KEY", "o" * 32)
    def test_google_oauth_state_still_round_trips_for_authenticated_user(self):
        app = self.make_app(secret="o" * 32)
        with app.test_request_context("/"):
            session["user_id"] = 17
            state = _state_for_business(23)
            self.assertEqual(
                {"business_id": 23, "user_id": 17},
                _load_state(state),
            )


if __name__ == "__main__":
    unittest.main()
