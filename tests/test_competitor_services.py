import unittest
from unittest.mock import Mock, patch

import requests

from app.services.competitor_service import comparison_summary, refresh_allowed
from app.services.google_places_service import (
    PlacesConfigurationError, PlacesPermissionError, PlacesQuotaError,
    clamp_radius, discover_competitors, validate_place_id,
)


SOURCE = {"business_id": 9, "name": "ReviewGrow Hotel", "google_place_id": "own-place",
          "latitude": 11.0, "longitude": 77.0, "primary_category": "hotel", "business_type": "hotel"}


def response(status=200, payload=None):
    result = Mock(status_code=status, ok=200 <= status < 300)
    result.json.return_value = payload or {}
    return result


class PlacesConfigurationTests(unittest.TestCase):
    def test_radius_is_clamped(self):
        self.assertEqual(clamp_radius(100), 500)
        self.assertEqual(clamp_radius(999999), 20000)

    def test_place_id_validation(self):
        self.assertEqual(validate_place_id("ChIJ_abc-123"), "ChIJ_abc-123")
        with self.assertRaises(ValueError): validate_place_id("https://evil.example/place")

    @patch("app.services.google_places_service.Config.GOOGLE_PLACES_API_KEY", "")
    def test_missing_key_is_safe(self):
        with self.assertRaises(PlacesConfigurationError): discover_competitors(SOURCE)

    @patch("app.services.google_places_service.Config.GOOGLE_PLACES_API_KEY", "test-key")
    def test_missing_coordinates_are_rejected(self):
        with self.assertRaises(PlacesConfigurationError): discover_competitors({**SOURCE, "latitude": None})


class PlacesDiscoveryTests(unittest.TestCase):
    def places(self):
        return {"places": [
            {"id": "own-place", "displayName": {"text": "Own"}, "location": {"latitude": 11.0, "longitude": 77.0}, "primaryType": "hotel", "userRatingCount": 100},
            {"id": "near-hotel", "displayName": {"text": "Nearby Hotel"}, "formattedAddress": "One Road", "location": {"latitude": 11.001, "longitude": 77.001}, "primaryType": "hotel", "types": ["hotel"], "rating": 4.5, "userRatingCount": 500, "businessStatus": "OPERATIONAL", "googleMapsUri": "https://maps.google.com/a"},
            {"id": "far-hotel", "displayName": {"text": "Far Hotel"}, "location": {"latitude": 11.1, "longitude": 77.1}, "primaryType": "hotel", "types": ["hotel"], "rating": 4.8, "userRatingCount": 20, "businessStatus": "OPERATIONAL"},
            {"id": "closed", "displayName": {"text": "Closed"}, "location": {"latitude": 11.0, "longitude": 77.0}, "primaryType": "hotel", "rating": 5, "userRatingCount": 1000, "businessStatus": "CLOSED_PERMANENTLY"},
            {"id": "tiny", "displayName": {"text": "Tiny"}, "location": {"latitude": 11.0, "longitude": 77.0}, "primaryType": "hotel", "rating": 5, "userRatingCount": 2, "businessStatus": "OPERATIONAL"},
        ]}

    @patch("app.services.google_places_service.Config.GOOGLE_PLACES_API_KEY", "test-key")
    def test_filters_own_closed_and_low_volume_and_ranks_nearby(self):
        client = Mock(); client.post.return_value = response(payload=self.places())
        result = discover_competitors(SOURCE, radius_meters=20000, session=client)
        self.assertEqual([item["google_place_id"] for item in result["candidates"]], ["near-hotel", "far-hotel"])
        self.assertTrue(result["candidates"][0]["category_match"])
        headers = client.post.call_args.kwargs["headers"]
        self.assertIn("places.id", headers["X-Goog-FieldMask"])
        self.assertNotIn("reviews", headers["X-Goog-FieldMask"])

    @patch("app.services.google_places_service.Config.GOOGLE_PLACES_API_KEY", "test-key")
    def test_quota_and_permission_errors_are_mapped(self):
        client = Mock(); client.post.return_value = response(429)
        with self.assertRaises(PlacesQuotaError): discover_competitors(SOURCE, session=client)
        client.post.return_value = response(403)
        with self.assertRaises(PlacesPermissionError): discover_competitors(SOURCE, session=client)

    @patch("app.services.google_places_service.Config.GOOGLE_PLACES_API_KEY", "test-key")
    def test_timeout_does_not_expose_key(self):
        client = Mock(); client.post.side_effect = requests.Timeout()
        from app.services.google_places_service import PlacesTemporaryError
        with self.assertRaises(PlacesTemporaryError): discover_competitors(SOURCE, session=client)


class CompetitorComparisonTests(unittest.TestCase):
    def test_current_comparison(self):
        competitors = [{"competitor_name": "A", "rating": 4.2, "user_rating_count": 100},
                       {"competitor_name": "B", "rating": 4.8, "user_rating_count": 300}]
        result = comparison_summary(4.7, 250, competitors)
        self.assertEqual(result["average_rating"], 4.5)
        self.assertEqual(result["rating_gap"], .2)
        self.assertEqual(result["average_review_count"], 200)
        self.assertEqual(result["review_count_gap"], 50)
        self.assertEqual(result["highest_rated"]["competitor_name"], "B")
        self.assertEqual(result["rank"], 2)

    def test_missing_ratings_are_ignored(self):
        result = comparison_summary(None, 10, [{"rating": None, "user_rating_count": None}])
        self.assertIsNone(result["average_rating"])
        self.assertIsNone(result["rank"])


if __name__ == "__main__": unittest.main()
