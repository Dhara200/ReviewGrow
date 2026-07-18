import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from app.services.google_business_service import GoogleBusinessError
from app.services.google_review_sync_execution_service import run_google_review_sync


class GoogleReviewSyncExecutionServiceTests(unittest.TestCase):
    @patch("app.services.google_review_sync_execution_service.synchronize_google_reviews")
    @patch("app.services.google_review_sync_execution_service.ensure_valid_google_connection_token")
    @patch("app.services.google_review_sync_execution_service._load_owned_google_connection")
    def test_sync_execution_uses_explicit_ids_without_flask_session(
        self,
        load_connection,
        ensure_token,
        synchronize,
    ):
        connection = {
            "id": 3,
            "user_id": 7,
            "business_id": 9,
            "google_account_id": "accounts/1",
            "google_location_id": "locations/2",
            "access_token": "token",
            "token_expiry": datetime.utcnow() + timedelta(minutes=30),
        }
        load_connection.return_value = connection
        ensure_token.return_value = connection
        synchronize.return_value = {
            "fetched_count": 8,
            "inserted_count": 3,
            "updated_count": 1,
        }

        result = run_google_review_sync(7, 9)

        self.assertEqual(8, result["fetched_count"])
        load_connection.assert_called_once_with(7, 9)
        ensure_token.assert_called_once_with(connection)
        synchronize.assert_called_once_with(connection, allow_internal_api_retry=False)

    @patch("app.services.google_review_sync_execution_service.get_connection")
    def test_missing_owned_connection_fails_validation(self, get_connection):
        cursor = unittest.mock.MagicMock()
        cursor.fetchone.return_value = None
        database = unittest.mock.MagicMock()
        database.cursor.return_value = cursor
        get_connection.return_value = database

        with self.assertRaises(GoogleBusinessError):
            run_google_review_sync(7, 9)


if __name__ == "__main__":
    unittest.main()
