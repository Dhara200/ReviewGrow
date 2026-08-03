import unittest
from datetime import date, timedelta

from app.services.competitor_history_analytics_service import (
    build_history_from_rows,
    rating_gap_interpretation,
)


COMPETITORS = [
    {"id": 2, "competitor_name": "Alpha"},
    {"id": 3, "competitor_name": "Beta"},
]


def row(day, key, rating, count, name=None, job=1):
    return {
        "capture_date": day, "subject_key": key,
        "subject_type": "customer" if key == "customer" else "competitor",
        "competitor_id": None if key == "customer" else int(key.split(":")[1]),
        "subject_name": name or ("Own" if key == "customer" else key),
        "rating": rating, "user_rating_count": count,
        "business_status": "OPERATIONAL", "refresh_job_id": job,
    }


def history(days, missing=None):
    missing = missing or set(); rows = []; start = date(2026, 1, 1)
    for offset in range(days):
        day = start + timedelta(days=offset)
        rows.append(row(day, "customer", 4.2 + offset / 100, 100 + offset, "Own", offset))
        if ("competitor:2", offset) not in missing:
            rows.append(row(day, "competitor:2", 4.4, 120 + offset * 2, "Alpha", offset))
        if ("competitor:3", offset) not in missing:
            rows.append(row(day, "competitor:3", 4.1, 90 + offset, "Beta", offset))
    return rows


class ReadinessTests(unittest.TestCase):
    def test_zero_one_two_six_seven_and_thirty_day_stages(self):
        expected = [(0, 0), (1, 1), (2, 2), (6, 2), (7, 3), (30, 4)]
        for days, stage in expected:
            with self.subTest(days=days):
                result = build_history_from_rows(history(days), COMPETITORS)
                self.assertEqual(stage, result["readiness"]["stage"])

    def test_window_unlocking_and_invalid_fallback(self):
        seven = build_history_from_rows(history(7), COMPETITORS, "7")
        self.assertEqual("7", seven["selected_window"])
        unsupported = build_history_from_rows(history(7), COMPETITORS, "30")
        self.assertEqual("available", unsupported["selected_window"])
        invalid = build_history_from_rows(history(7), COMPETITORS, "anything")
        self.assertEqual("available", invalid["requested_window"])

    def test_ninety_day_window_requires_sufficient_coverage(self):
        result = build_history_from_rows(history(90), COMPETITORS, "90")
        self.assertEqual("90", result["selected_window"])
        self.assertTrue(next(item for item in result["windows"] if item["value"] == "90")["enabled"])


class AlignmentAndCalculationTests(unittest.TestCase):
    def test_missing_competitor_value_remains_null_and_average_excludes_it(self):
        result = build_history_from_rows(history(7, {("competitor:2", 3)}), COMPETITORS, "7")
        alpha = next(item for item in result["rating_series"]["subjects"] if item["subject_key"] == "competitor:2")
        self.assertIsNone(alpha["rating"][3])
        self.assertEqual(1, result["data_quality"]["selected_average_contributor_count_by_date"][3])
        self.assertIn("2026-01-04", result["data_quality"]["partial_refresh_dates"])

    def test_current_baseline_growth_and_actual_span(self):
        result = build_history_from_rows(history(7), COMPETITORS, "7")
        self.assertEqual(6, result["period"]["span_days"])
        self.assertEqual(6, result["summary"]["review_growth"])
        self.assertAlmostEqual(.06, result["summary"]["rating_change"], places=2)

    def test_review_count_decrease_is_flagged_not_hidden(self):
        rows = history(3)
        for item in rows:
            if item["subject_key"] == "customer" and item["capture_date"] == date(2026, 1, 3):
                item["user_rating_count"] = 80
        result = build_history_from_rows(rows, COMPETITORS)
        self.assertTrue(result["data_quality"]["anomalous_review_count_decreases"])
        self.assertEqual(-20, result["summary"]["review_growth"])

    def test_rating_rank_uses_review_count_then_subject_key(self):
        day1, day2 = date(2026, 1, 1), date(2026, 1, 2)
        rows = [
            row(day1, "customer", 4.5, 100), row(day1, "competitor:2", 4.5, 110), row(day1, "competitor:3", 4.5, 90),
            row(day2, "customer", 4.6, 100), row(day2, "competitor:2", 4.5, 120), row(day2, "competitor:3", 4.5, 100),
        ]
        result = build_history_from_rows(rows, COMPETITORS)
        self.assertEqual([2, 1], result["rank_series"]["customer"])
        self.assertEqual(1, result["summary"]["rank_movement"])

    def test_removed_competitor_rows_are_excluded(self):
        result = build_history_from_rows(history(7), [COMPETITORS[0]], "7")
        keys = {item["subject_key"] for item in result["subjects"]}
        self.assertNotIn("competitor:3", keys)

    def test_gap_interpretations_use_correct_sign_language(self):
        self.assertIn("narrowed", rating_gap_interpretation(-.4, -.2))
        self.assertIn("lead increased", rating_gap_interpretation(.1, .2))
        self.assertIn("widened", rating_gap_interpretation(-.2, -.4))
        self.assertIn("stable", rating_gap_interpretation(0, .01))


class InsightsTests(unittest.TestCase):
    def test_low_history_has_no_strategic_claims(self):
        self.assertEqual([], build_history_from_rows(history(1), COMPETITORS)["insights"])

    def test_insights_are_deterministic_and_contain_no_outcome_promises(self):
        result = build_history_from_rows(history(14), COMPETITORS)
        text = " ".join(item["title"] + " " + item["explanation"] for item in result["insights"]).lower()
        self.assertNotIn("revenue", text)
        self.assertNotIn("guarantee", text)
        self.assertEqual(len({item["id"] for item in result["insights"]}), len(result["insights"]))
        self.assertTrue(all("span_days" in item for item in result["insights"]))

    def test_sharp_rating_anomaly_suppresses_strong_insights(self):
        rows = history(7)
        for item in rows:
            if item["subject_key"] == "customer" and item["capture_date"] == date(2026, 1, 7):
                item["rating"] = 5.0
        result = build_history_from_rows(rows, COMPETITORS)
        self.assertTrue(result["data_quality"]["anomalous_rating_changes"])
        self.assertEqual([], result["insights"])


if __name__ == "__main__":
    unittest.main()
