import unittest
from datetime import date, datetime, timezone

from app.services.consultant_analytics_service import (
    build_trend_buckets,
    calculate_health_score,
    normalize_topic,
    prioritize_actions,
    resolve_period,
)


class ConsultantPeriodTests(unittest.TestCase):
    def test_supported_periods_have_equivalent_previous_window(self):
        now = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
        for days in (7, 30, 90, 365):
            with self.subTest(days=days):
                period = resolve_period(str(days), now)
                self.assertEqual(period["days"], days)
                self.assertEqual((period["current_end"] - period["current_start"]).days, days)
                self.assertEqual((period["previous_end"] - period["previous_start"]).days, days)
                self.assertEqual(period["previous_end"], period["current_start"])

    def test_invalid_period_falls_back_to_30_days(self):
        self.assertEqual(resolve_period("999")["days"], 30)
        self.assertEqual(resolve_period("invalid")["days"], 30)


class ConsultantHealthTests(unittest.TestCase):
    def metrics(self, rating, positive=80, neutral=10, response=90, unanswered=0):
        return {"total_reviews": 20, "average_rating": rating, "analysed_reviews": 20,
                "positive_percentage": positive, "neutral_percentage": neutral,
                "unanswered_negative_reviews": unanswered, "response_rate": response,
                "response_comparison_available": True, "analysis_coverage": 100}

    def test_health_labels_cover_boundaries(self):
        self.assertEqual(calculate_health_score(self.metrics(5))["status"], "Excellent")
        self.assertIn(calculate_health_score(self.metrics(4, 70, 15, 70))["status"], {"Strong", "Excellent"})
        self.assertEqual(calculate_health_score(self.metrics(3, 45, 20, 40, 5))["status"], "Needs Attention")
        self.assertEqual(calculate_health_score(self.metrics(1, 10, 10, 0, 15))["status"], "Critical")

    def test_missing_data_returns_pending(self):
        result = calculate_health_score({"total_reviews": 0})
        self.assertIsNone(result["score"])
        self.assertEqual(result["status"], "Pending")

    def test_score_is_clamped(self):
        result = calculate_health_score(self.metrics(7, 150, 0, 200))
        self.assertLessEqual(result["score"], 100)


class ConsultantTopicAndTrendTests(unittest.TestCase):
    def test_topic_normalization(self):
        self.assertEqual(normalize_topic("Wi-Fi"), "wifi")
        self.assertEqual(normalize_topic("customer_service"), "service")

    def test_daily_buckets_include_empty_dates(self):
        period = resolve_period(7, datetime(2026, 8, 3, 12, tzinfo=timezone.utc))
        rows = [{"review_day": date(2026, 8, 1), "review_count": 2, "rating": 4.5,
                 "positive": 2, "neutral": 0, "negative": 0, "answered": 1}]
        points = build_trend_buckets(rows, period)
        self.assertGreaterEqual(len(points), 7)
        self.assertEqual([point["label"] for point in points], sorted(point["label"] for point in points))
        self.assertEqual(sum(point["review_count"] for point in points), 2)

    def test_empty_period_contains_real_zero_buckets(self):
        period = resolve_period(30, datetime(2026, 8, 3, 12, tzinfo=timezone.utc))
        self.assertTrue(all(point["review_count"] == 0 for point in build_trend_buckets([], period)))


class ConsultantActionTests(unittest.TestCase):
    def test_repeated_complaint_and_unanswered_reviews_are_prioritized(self):
        kpis = {"unanswered_reviews": {"current": 4}}
        sentiment = {"negative": 3, "negative_change": {"comparison_available": False}}
        topics = [{"topic": "wifi", "status": "Critical", "mention_count": 6,
                   "negative_percentage": 83.3, "positive_percentage": 0,
                   "negative_count": 5, "positive_count": 0, "mention_change": 2,
                   "confidence": "medium"}]
        actions = prioritize_actions(kpis, sentiment, topics, [])
        self.assertEqual(actions[0]["priority"], "High")
        self.assertLessEqual(len(actions), 5)
        self.assertEqual(len({item["id"] for item in actions}), len(actions))
        self.assertFalse(any("revenue" in item["expected_impact"].lower() for item in actions))


if __name__ == "__main__":
    unittest.main()
