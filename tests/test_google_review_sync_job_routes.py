import unittest
from datetime import datetime
from unittest.mock import patch

from flask import Flask

from app.routes.google_business import google_business_bp


class GoogleReviewSyncJobRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        self.app.register_blueprint(google_business_bp)
        self.client = self.app.test_client()

    def login(self, user_id=7):
        with self.client.session_transaction() as session:
            session["user_id"] = user_id
            session["role"] = "owner"

    def enqueue(self, business_id=9):
        return self.client.post(
            f"/businesses/{business_id}/google/review-sync-jobs",
            headers={"Accept": "application/json"},
        )

    @patch("app.services.subscription_service.has_active_subscription", return_value=True)
    @patch("app.routes.google_business._get_connection_row")
    @patch("app.routes.google_business.user_owns_business", return_value=True)
    @patch("app.routes.google_business.google_review_sync_jobs.create_job", return_value=(41, True))
    def test_owner_can_enqueue_new_job(
        self,
        create_job,
        _owns_business,
        get_connection,
        _has_subscription,
    ):
        self.login()
        get_connection.return_value = {
            "google_account_id": "accounts/1",
            "google_location_id": "locations/2",
        }

        response = self.enqueue()

        self.assertEqual(202, response.status_code)
        self.assertEqual({
            "success": True,
            "job_id": 41,
            "created": True,
            "status": "pending",
            "message": "Review synchronization has been queued.",
        }, response.get_json())
        create_job.assert_called_once_with(7, 9)

    @patch("app.services.subscription_service.has_active_subscription", return_value=True)
    @patch("app.routes.google_business._get_connection_row")
    @patch("app.routes.google_business.user_owns_business", return_value=True)
    @patch("app.routes.google_business.google_review_sync_jobs.create_job", return_value=(23, False))
    def test_enqueue_returns_existing_active_job(
        self,
        create_job,
        _owns_business,
        get_connection,
        _has_subscription,
    ):
        self.login()
        get_connection.return_value = {
            "google_account_id": "accounts/1",
            "google_location_id": "locations/2",
        }

        response = self.enqueue()

        self.assertEqual(200, response.status_code)
        self.assertEqual(23, response.get_json()["job_id"])
        self.assertFalse(response.get_json()["created"])
        self.assertEqual("already_running", response.get_json()["status"])
        create_job.assert_called_once_with(7, 9)

    def test_unauthenticated_enqueue_is_rejected(self):
        response = self.enqueue()

        self.assertEqual(302, response.status_code)
        self.assertTrue(response.location.endswith("/login-page"))

    @patch("app.services.subscription_service.has_active_subscription", return_value=True)
    @patch("app.routes.google_business._get_connection_row")
    @patch("app.routes.google_business.user_owns_business", return_value=False)
    @patch("app.routes.google_business.google_review_sync_jobs.create_job")
    def test_user_cannot_enqueue_for_another_users_business(
        self,
        create_job,
        _owns_business,
        get_connection,
        _has_subscription,
    ):
        self.login()

        response = self.enqueue()

        self.assertEqual(403, response.status_code)
        create_job.assert_not_called()
        get_connection.assert_not_called()

    @patch("app.services.subscription_service.has_active_subscription", return_value=True)
    @patch("app.routes.google_business.sync_google_reviews")
    @patch("app.routes.google_business._valid_connection_token")
    @patch("app.routes.google_business._get_connection_row")
    @patch("app.routes.google_business.user_owns_business", return_value=True)
    @patch("app.routes.google_business.google_review_sync_jobs.create_job", return_value=(41, True))
    def test_enqueue_does_not_call_google_or_synchronize_reviews(
        self,
        _create_job,
        _owns_business,
        get_connection,
        refresh_token,
        sync_reviews,
        _has_subscription,
    ):
        self.login()
        get_connection.return_value = {
            "google_account_id": "accounts/1",
            "google_location_id": "locations/2",
        }

        response = self.enqueue()

        self.assertEqual(202, response.status_code)
        refresh_token.assert_not_called()
        sync_reviews.assert_not_called()

    @patch("app.services.subscription_service.has_active_subscription", return_value=True)
    @patch("app.routes.google_business.google_review_sync_jobs.get_job", return_value=None)
    def test_user_cannot_view_another_users_job(self, get_job, _has_subscription):
        self.login(user_id=7)

        response = self.client.get("/google-review-sync-jobs/41/status")

        self.assertEqual(404, response.status_code)
        get_job.assert_called_once_with(41, user_id=7)

    @patch("app.services.subscription_service.has_active_subscription", return_value=True)
    @patch("app.routes.google_business.google_review_sync_jobs.get_job")
    def test_owner_can_retrieve_job_status(self, get_job, _has_subscription):
        self.login(user_id=7)
        created_at = datetime(2026, 7, 17, 8, 30)
        get_job.return_value = {
            "id": 41,
            "business_id": 9,
            "status": "processing",
            "fetched_count": 10,
            "inserted_count": 4,
            "updated_count": 2,
            "error_message": "must not be exposed",
            "created_at": created_at,
            "started_at": created_at,
            "completed_at": None,
        }

        response = self.client.get("/google-review-sync-jobs/41/status")

        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertEqual(41, payload["job_id"])
        self.assertEqual("processing", payload["status"])
        self.assertNotIn("error_message", payload)
        get_job.assert_called_once_with(41, user_id=7)

    @patch("app.services.subscription_service.has_active_subscription", return_value=True)
    @patch("app.routes.google_business.google_review_sync_jobs.get_job", return_value=None)
    def test_missing_job_returns_404(self, _get_job, _has_subscription):
        self.login()

        response = self.client.get("/google-review-sync-jobs/999/status")

        self.assertEqual(404, response.status_code)

    @patch("app.services.subscription_service.has_active_subscription", return_value=True)
    @patch("app.routes.google_business.google_review_sync_jobs.get_active_job")
    @patch("app.routes.google_business.user_owns_business", return_value=True)
    def test_owner_can_resume_active_job(self, _owns_business, get_active_job, _subscription):
        self.login(user_id=7)
        get_active_job.return_value = {
            "id": 41, "business_id": 9, "status": "processing",
            "fetched_count": 0, "inserted_count": 0, "updated_count": 0,
            "created_at": datetime(2026, 7, 17, 8, 30),
            "started_at": datetime(2026, 7, 17, 8, 31), "completed_at": None,
        }

        response = self.client.get("/businesses/9/google/review-sync-jobs/active")

        self.assertEqual(200, response.status_code)
        self.assertEqual(41, response.get_json()["job_id"])
        get_active_job.assert_called_once_with(9, user_id=7)

    @patch("app.services.subscription_service.has_active_subscription", return_value=True)
    @patch("app.routes.google_business.google_review_sync_jobs.get_active_job")
    @patch("app.routes.google_business.user_owns_business", return_value=False)
    def test_user_cannot_discover_another_users_active_job(
        self, _owns_business, get_active_job, _subscription
    ):
        self.login(user_id=7)
        response = self.client.get("/businesses/9/google/review-sync-jobs/active")
        self.assertEqual(403, response.status_code)
        get_active_job.assert_not_called()

    @patch("app.services.subscription_service.has_active_subscription", return_value=True)
    @patch("app.routes.google_business.google_review_sync_jobs.get_active_job", return_value=None)
    @patch("app.routes.google_business.user_owns_business", return_value=True)
    def test_missing_active_job_returns_404(self, _owns_business, _get_active, _subscription):
        self.login(user_id=7)
        response = self.client.get("/businesses/9/google/review-sync-jobs/active")
        self.assertEqual(404, response.status_code)

    def test_synchronous_fallback_route_remains_registered(self):
        rules = {
            (rule.rule, tuple(sorted(rule.methods - {"HEAD", "OPTIONS"})))
            for rule in self.app.url_map.iter_rules()
        }
        self.assertIn(("/businesses/<int:business_id>/google/sync-reviews", ("POST",)), rules)


if __name__ == "__main__":
    unittest.main()
