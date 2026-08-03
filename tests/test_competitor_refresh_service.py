import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.services import competitor_refresh_service as service
from app.services.google_places_service import PlacesTemporaryError


def details(place_id, name):
    return {
        "google_place_id": place_id, "name": name, "formatted_address": "Address",
        "latitude": 12.1, "longitude": 80.1, "primary_type": "hotel",
        "types": ["hotel"], "rating": 4.5, "user_rating_count": 100,
        "business_status": "OPERATIONAL", "google_maps_url": "", "distance_meters": 10,
    }


class CompetitorRefreshExecutionTests(unittest.TestCase):
    def setUp(self):
        self.source = {"name": "Own", "google_place_id": "own", "latitude": 12, "longitude": 80}
        self.competitors = [
            {"id": 8, "google_place_id": "other-a"},
            {"id": 9, "google_place_id": "other-b"},
        ]
        self.job = {"id": 44, "business_id": 3, "attempt_count": 1, "max_attempts": 3}

    @patch.object(service, "_persist_competitor_snapshot", return_value=True)
    @patch.object(service, "_persist_customer_snapshot", return_value=True)
    @patch.object(service, "_load_targets")
    @patch.object(service, "get_place_details")
    def test_refreshes_customer_competitors_and_snapshots(self, get_details, load, customer, competitor):
        load.return_value = (self.source, self.competitors)
        get_details.side_effect = [details("own", "Own"), details("other-a", "A"), details("other-b", "B")]
        result = service.execute_competitor_refresh_job(self.job, ownership_check=lambda: True)
        self.assertEqual("completed", result["outcome"])
        self.assertTrue(result["customer_refreshed"])
        self.assertEqual(2, result["competitors_refreshed"])
        self.assertEqual(3, result["snapshot_rows_created"])

    @patch.object(service, "_persist_competitor_snapshot", return_value=True)
    @patch.object(service, "_persist_customer_snapshot", return_value=True)
    @patch.object(service, "_load_targets")
    @patch.object(service, "get_place_details")
    def test_partial_failure_preserves_successes(self, get_details, load, customer, competitor):
        load.return_value = (self.source, self.competitors)
        get_details.side_effect = [details("own", "Own"), PlacesTemporaryError("temporary"), details("other-b", "B")]
        result = service.execute_competitor_refresh_job(self.job)
        self.assertEqual("partially_completed", result["outcome"])
        self.assertEqual(1, result["competitors_failed"])
        self.assertNotIn("temporary", str(result["failure_summaries"]))

    @patch.object(service, "_load_targets")
    @patch.object(service, "get_place_details", side_effect=PlacesTemporaryError("temporary"))
    def test_total_temporary_failure_is_retryable(self, get_details, load):
        load.return_value = (self.source, self.competitors)
        with self.assertRaises(PlacesTemporaryError):
            service.execute_competitor_refresh_job(self.job)

    def test_refresh_window_key_is_stable_within_window(self):
        instant = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
        self.assertEqual(service._naive_utc(instant), datetime(2026, 8, 3, 12))


class CompetitorRefreshWorkerTests(unittest.TestCase):
    @patch("worker.complete_competitor_refresh_job", return_value=True)
    @patch("worker.execute_competitor_refresh_job", return_value={"outcome": "completed"})
    def test_worker_completes_owned_refresh(self, execute, complete):
        import worker
        heartbeat = type("Heartbeat", (), {"ownership_lost": False})()
        job = {"id": 5, "business_id": 4, "attempt_count": 1, "max_attempts": 3}
        self.assertTrue(worker._process_competitor_refresh_job(job, heartbeat))
        complete.assert_called_once_with(5, worker.WORKER_ID, {"outcome": "completed"})

    @patch("worker.enqueue_due_competitor_refresh_jobs", return_value={"eligible": 1, "created": 1, "reused": 0})
    def test_scheduler_interval_prevents_duplicate_scan(self, enqueue):
        import worker
        worker._next_competitor_schedule_check = 0
        worker._run_competitor_scheduler_if_due(now=100)
        worker._run_competitor_scheduler_if_due(now=101)
        enqueue.assert_called_once()


if __name__ == "__main__":
    unittest.main()
