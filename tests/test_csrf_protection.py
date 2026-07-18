import unittest

from flask import Flask, jsonify, request, session

from app.services.csrf_service import CSRF_SESSION_KEY, get_csrf_token, init_csrf


class CsrfProtectionTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY="csrf-test-secret")
        init_csrf(self.app)

        @self.app.get("/token")
        def token():
            return jsonify({"token": get_csrf_token()})

        @self.app.get("/status")
        def status():
            return jsonify({"authenticated": "user_id" in session})

        @self.app.post("/json-mutation")
        def json_mutation():
            return jsonify({"success": True})

        @self.app.post("/form-mutation")
        def form_mutation():
            return "updated"

        self.client = self.app.test_client()

    def login(self, client=None, user_id=7):
        client = client or self.client
        with client.session_transaction() as active_session:
            active_session["user_id"] = user_id
        return client.get("/token").get_json()["token"]

    def test_authenticated_json_post_without_token_is_rejected_safely(self):
        self.login()

        response = self.client.post(
            "/json-mutation",
            json={},
            headers={"Accept": "application/json"},
        )

        self.assertEqual(403, response.status_code)
        self.assertEqual(False, response.get_json()["success"])
        self.assertNotIn("Traceback", response.get_data(as_text=True))

    def test_invalid_and_other_session_tokens_are_rejected(self):
        first_token = self.login()
        other_client = self.app.test_client()
        other_token = self.login(other_client, user_id=8)
        self.assertNotEqual(first_token, other_token)

        for invalid_token in ("invalid", other_token):
            with self.subTest(token=invalid_token):
                response = self.client.post(
                    "/json-mutation",
                    json={},
                    headers={"X-CSRF-Token": invalid_token},
                )
                self.assertEqual(403, response.status_code)

    def test_valid_token_supports_multiple_tabs_in_same_session(self):
        token = self.login()

        first = self.client.post(
            "/json-mutation", json={}, headers={"X-CSRF-Token": token}
        )
        second = self.client.post(
            "/json-mutation", json={}, headers={"X-CSRF-Token": token}
        )

        self.assertEqual(200, first.status_code)
        self.assertEqual(200, second.status_code)

    def test_form_token_succeeds_and_failure_has_no_stack_trace(self):
        token = self.login()

        valid = self.client.post("/form-mutation", data={"csrf_token": token})
        invalid = self.client.post("/form-mutation", data={"csrf_token": "bad"})

        self.assertEqual(200, valid.status_code)
        self.assertEqual(403, invalid.status_code)
        self.assertNotIn("Traceback", invalid.get_data(as_text=True))

    def test_get_is_accessible_without_csrf(self):
        self.login()
        response = self.client.get("/status")
        self.assertEqual(200, response.status_code)

    def test_session_reset_invalidates_prior_token(self):
        old_token = self.login()
        with self.client.session_transaction() as active_session:
            active_session.clear()
            active_session["user_id"] = 7
        new_token = self.client.get("/token").get_json()["token"]

        self.assertNotEqual(old_token, new_token)
        rejected = self.client.post(
            "/json-mutation", json={}, headers={"X-CSRF-Token": old_token}
        )
        self.assertEqual(403, rejected.status_code)

    def test_same_origin_header_is_accepted_and_cross_origin_is_rejected(self):
        token = self.login()
        valid = self.client.post(
            "/json-mutation",
            json={},
            headers={"X-CSRF-Token": token, "Origin": "http://localhost"},
        )
        invalid = self.client.post(
            "/json-mutation",
            json={},
            headers={"X-CSRF-Token": token, "Origin": "https://attacker.example"},
        )

        self.assertEqual(200, valid.status_code)
        self.assertEqual(403, invalid.status_code)

    def test_unauthenticated_post_behavior_is_not_intercepted(self):
        response = self.client.post("/form-mutation")
        self.assertEqual(200, response.status_code)

    def test_tokens_are_cryptographically_sized_and_stored_in_session(self):
        token = self.login()
        self.assertGreaterEqual(len(token), 40)
        with self.client.session_transaction() as active_session:
            self.assertEqual(token, active_session[CSRF_SESSION_KEY])


if __name__ == "__main__":
    unittest.main()
