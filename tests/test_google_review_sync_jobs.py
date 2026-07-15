import unittest
from unittest.mock import patch

from app.app import app


class GoogleReviewSyncRouteTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, SECRET_KEY="test-secret")
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = 7
            session["role"] = "admin"

    @patch("app.routes.google_business.create_google_review_sync_job", return_value=(41, True))
    @patch("app.routes.google_business._get_connection_row")
    def test_sync_endpoint_only_queues_job(self, connection_mock, create_job_mock):
        connection_mock.return_value = {
            "id": 3,
            "business_id": 9,
            "google_account_id": "accounts/1",
            "google_location_id": "locations/2",
        }

        response = self.client.post(
            "/businesses/9/google/sync-reviews",
            headers={"Accept": "application/json"},
        )

        self.assertEqual(202, response.status_code)
        self.assertEqual("pending", response.get_json()["status"])
        self.assertEqual(41, response.get_json()["job_id"])
        create_job_mock.assert_called_once_with(7, 9)

    @patch("app.routes.google_business.get_google_review_sync_job")
    def test_status_endpoint_returns_owned_job_state(self, get_job_mock):
        get_job_mock.return_value = {
            "job_id": 41,
            "business_id": 9,
            "status": "processing",
        }

        response = self.client.get("/google-review-sync-jobs/41/status")

        self.assertEqual(200, response.status_code)
        self.assertEqual("processing", response.get_json()["status"])
        get_job_mock.assert_called_once_with(41, 7, is_admin=True)


if __name__ == "__main__":
    unittest.main()
