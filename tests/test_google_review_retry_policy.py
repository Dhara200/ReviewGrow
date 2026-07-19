import unittest
from unittest.mock import MagicMock, patch

from app.services.google_business_service import (
    GoogleBusinessError,
    GoogleQuotaError,
    GoogleTransientError,
    api_get,
    list_reviews,
)
from app.services.review_sync_service import sync_google_reviews


def google_response(status_code, payload=None):
    response = MagicMock()
    response.status_code = status_code
    response.ok = 200 <= status_code < 300
    response.headers = {}
    response.url = "https://mybusiness.googleapis.com/v4/accounts/1/locations/2/reviews"
    response.json.return_value = payload or {}
    response.text = ""
    return response


class GoogleReviewRetryPolicyTests(unittest.TestCase):
    @patch("app.services.google_business_service.time.sleep")
    @patch("app.services.google_business_service.requests.get")
    def test_review_policy_429_uses_one_request_and_no_api_sleep(self, get, sleep):
        get.return_value = google_response(429, {"error": {"status": "RESOURCE_EXHAUSTED"}})

        with self.assertRaises(GoogleQuotaError):
            api_get("token", "https://example.test/reviews", allow_internal_retry=False)

        get.assert_called_once()
        sleep.assert_not_called()

    @patch("app.services.google_business_service.time.sleep")
    @patch("app.services.google_business_service.requests.get")
    def test_review_policy_5xx_uses_one_request_and_no_api_sleep(self, get, sleep):
        for status in (500, 502, 503, 504):
            with self.subTest(status=status):
                get.reset_mock()
                sleep.reset_mock()
                get.return_value = google_response(status)
                with self.assertRaises(GoogleTransientError):
                    api_get(
                        "token",
                        "https://example.test/reviews",
                        allow_internal_retry=False,
                    )
                get.assert_called_once()
                sleep.assert_not_called()

    @patch("app.services.google_business_service.api_get")
    def test_review_pagination_explicitly_propagates_single_attempt_policy(self, api):
        api.side_effect = [
            {"reviews": [{"reviewId": "one"}], "nextPageToken": "next"},
            {"reviews": [{"reviewId": "two"}]},
        ]

        reviews = list_reviews(
            "token", "accounts/1", "locations/2", allow_internal_retry=False
        )

        self.assertEqual(["one", "two"], [review["reviewId"] for review in reviews])
        self.assertEqual(2, api.call_count)
        for call in api.call_args_list:
            self.assertFalse(call.kwargs["allow_internal_retry"])

    @patch("app.services.google_business_service.time.sleep")
    @patch("app.services.google_business_service.requests.get")
    def test_unrelated_default_api_get_retains_internal_429_retry(self, get, sleep):
        get.side_effect = [
            google_response(429),
            google_response(200, {"accounts": []}),
        ]

        result = api_get("token", "https://example.test/accounts")

        self.assertEqual({"accounts": []}, result)
        self.assertEqual(2, get.call_count)
        sleep.assert_called_once_with(1)

    @patch("app.services.review_sync_service.list_reviews")
    def test_later_page_failure_performs_no_database_writes(self, list_reviews_mock):
        list_reviews_mock.side_effect = GoogleTransientError("later page failed")
        cursor = MagicMock()
        connection = {
            "access_token": "token",
            "google_account_id": "accounts/1",
            "google_location_id": "locations/2",
            "business_id": 9,
        }

        with self.assertRaises(GoogleTransientError):
            sync_google_reviews(cursor, connection, allow_internal_api_retry=False)

        cursor.execute.assert_not_called()
        list_reviews_mock.assert_called_once_with(
            "token",
            "accounts/1",
            "locations/2",
            allow_internal_retry=False,
        )

    @patch("app.services.google_business_service.requests.get")
    def test_validation_and_authorization_failures_remain_non_transient(self, get):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                get.reset_mock()
                get.return_value = google_response(status)
                with self.assertRaises(GoogleBusinessError) as raised:
                    api_get(
                        "token",
                        "https://example.test/reviews",
                        allow_internal_retry=False,
                    )
                self.assertNotIsInstance(raised.exception, GoogleTransientError)
                self.assertNotIsInstance(raised.exception, GoogleQuotaError)
                get.assert_called_once()


if __name__ == "__main__":
    unittest.main()
