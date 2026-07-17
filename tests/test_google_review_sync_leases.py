import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import worker


class GoogleReviewSyncLeaseTests(unittest.TestCase):
    def setUp(self):
        worker.shutdown_requested = False
        worker.shutdown_event.clear()

    def test_migration_adds_lease_columns_and_recovery_index(self):
        migration = Path(
            "database/migrations/20260717_002_google_review_sync_job_leases.sql"
        ).read_text(encoding="utf-8")

        self.assertIn("worker_id VARCHAR(255) NULL", migration)
        self.assertIn("lease_expires_at DATETIME(6) NULL", migration)
        self.assertIn("heartbeat_at DATETIME(6) NULL", migration)
        self.assertIn("(status, lease_expires_at)", migration)

    def test_worker_identifier_is_stable_and_bounded(self):
        self.assertLessEqual(len(worker.WORKER_ID), 255)
        self.assertEqual(2, worker.WORKER_ID.count(":"))

    def test_lease_configuration_is_valid(self):
        self.assertGreater(worker.Config.GOOGLE_REVIEW_SYNC_LEASE_SECONDS, 0)
        self.assertGreater(worker.Config.GOOGLE_REVIEW_SYNC_HEARTBEAT_SECONDS, 0)
        self.assertLess(
            worker.Config.GOOGLE_REVIEW_SYNC_HEARTBEAT_SECONDS,
            worker.Config.GOOGLE_REVIEW_SYNC_LEASE_SECONDS,
        )

    @patch("worker._process_google_review_sync_job", return_value=True)
    @patch("worker.claim_next_job", return_value=None)
    @patch("worker.google_review_sync_jobs")
    def test_heartbeat_processing_starts_only_after_owned_claim(
        self, job_service, _claim_ai, process_sync
    ):
        job = {"id": 41, "user_id": 7, "business_id": 9}
        job_service.get_oldest_pending_job.return_value = job
        job_service.claim_job.return_value = True

        worker.run_worker_iteration()

        job_service.claim_job.assert_called_once()
        process_sync.assert_called_once_with(job, worker.WORKER_ID)

        process_sync.reset_mock()
        job_service.claim_job.return_value = False
        worker.run_worker_iteration()
        process_sync.assert_not_called()

    @patch("worker.perform_google_review_post_sync")
    @patch("worker.run_google_review_sync")
    @patch("worker.google_review_sync_jobs")
    def test_heartbeat_renews_during_blocking_sync_and_is_cleaned_up(
        self, job_service, run_sync, _post_sync
    ):
        heartbeat_seen = threading.Event()
        job_service.heartbeat_job.side_effect = lambda *_args: heartbeat_seen.set() or True
        job_service.complete_job.return_value = True

        def blocking_sync(_user_id, _business_id):
            self.assertTrue(heartbeat_seen.wait(1))
            return {
                "fetched_count": 1,
                "inserted_count": 1,
                "updated_count": 0,
            }

        run_sync.side_effect = blocking_sync
        job = {"id": 41, "user_id": 7, "business_id": 9}

        with patch.object(worker.Config, "GOOGLE_REVIEW_SYNC_HEARTBEAT_SECONDS", 0.01):
            result = worker._process_google_review_sync_job(job, "worker-a")

        self.assertTrue(result)
        self.assertTrue(heartbeat_seen.is_set())
        job_service.heartbeat_job.assert_called_with(
            41, "worker-a", worker.Config.GOOGLE_REVIEW_SYNC_LEASE_SECONDS
        )
        self.assertFalse(any(
            thread.name == "google-review-sync-heartbeat-41" and thread.is_alive()
            for thread in threading.enumerate()
        ))

    @patch("worker.GoogleReviewSyncHeartbeat")
    @patch("worker.perform_google_review_post_sync")
    @patch("worker.run_google_review_sync")
    @patch("worker.google_review_sync_jobs")
    def test_ownership_loss_prevents_completion(
        self, job_service, run_sync, _post_sync, heartbeat_type
    ):
        heartbeat_type.return_value.ownership_lost = True
        run_sync.return_value = {
            "fetched_count": 1,
            "inserted_count": 1,
            "updated_count": 0,
        }

        result = worker._process_google_review_sync_job(
            {"id": 41, "user_id": 7, "business_id": 9}, "worker-a"
        )

        self.assertTrue(result)
        heartbeat_type.return_value.stop.assert_called_once()
        job_service.complete_job.assert_not_called()
        job_service.fail_job.assert_not_called()

    @patch("worker.GoogleReviewSyncHeartbeat")
    @patch("worker.run_google_review_sync", side_effect=RuntimeError("sync failed"))
    @patch("worker.google_review_sync_jobs")
    def test_ownership_loss_prevents_failure_overwrite(
        self, job_service, _run_sync, heartbeat_type
    ):
        heartbeat_type.return_value.ownership_lost = True

        result = worker._process_google_review_sync_job(
            {"id": 41, "user_id": 7, "business_id": 9}, "worker-a"
        )

        self.assertTrue(result)
        heartbeat_type.return_value.stop.assert_called_once()
        job_service.complete_job.assert_not_called()
        job_service.fail_job.assert_not_called()

    @patch("worker.perform_google_review_post_sync")
    @patch("worker.run_google_review_sync")
    @patch("worker.google_review_sync_jobs")
    def test_recovered_and_reclaimed_job_cannot_be_finalized_by_original_worker(
        self, job_service, run_sync, _post_sync
    ):
        run_sync.return_value = {
            "fetched_count": 1,
            "inserted_count": 1,
            "updated_count": 0,
        }
        job_service.complete_job.return_value = False

        result = worker._process_google_review_sync_job(
            {"id": 41, "user_id": 7, "business_id": 9}, "original-worker"
        )

        self.assertTrue(result)
        job_service.complete_job.assert_called_once_with(
            41, "original-worker", run_sync.return_value
        )
        job_service.fail_job.assert_not_called()

    @patch("worker.perform_google_review_post_sync")
    @patch("worker.run_google_review_sync")
    @patch("worker.google_review_sync_jobs")
    def test_shutdown_during_sync_does_not_stop_heartbeat_before_execution_finishes(
        self, job_service, run_sync, _post_sync
    ):
        heartbeat_seen = threading.Event()
        job_service.heartbeat_job.side_effect = lambda *_args: heartbeat_seen.set() or True
        job_service.complete_job.return_value = True

        def sync_during_shutdown(_user_id, _business_id):
            worker._request_shutdown(15, None)
            self.assertTrue(heartbeat_seen.wait(1))
            return {
                "fetched_count": 1,
                "inserted_count": 0,
                "updated_count": 0,
            }

        run_sync.side_effect = sync_during_shutdown
        with patch.object(worker.Config, "GOOGLE_REVIEW_SYNC_HEARTBEAT_SECONDS", 0.01):
            result = worker._process_google_review_sync_job(
                {"id": 41, "user_id": 7, "business_id": 9}, "worker-a"
            )

        self.assertTrue(result)
        self.assertTrue(heartbeat_seen.is_set())
        job_service.complete_job.assert_called_once()

    def test_heartbeat_database_error_is_contained_and_later_renewal_succeeds(self):
        job_service = MagicMock()
        attempts = {"count": 0}

        def heartbeat_result(*_args):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("database unavailable")
            return True

        job_service.heartbeat_job.side_effect = heartbeat_result
        heartbeat = worker.GoogleReviewSyncHeartbeat(
            job_service,
            {"id": 41, "business_id": 9},
            "worker-a",
            0.01,
            120,
        )

        heartbeat.start()
        deadline = time.monotonic() + 1
        while job_service.heartbeat_job.call_count < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        heartbeat.stop()

        self.assertGreaterEqual(job_service.heartbeat_job.call_count, 2)
        self.assertFalse(heartbeat.ownership_lost)

    def test_zero_row_heartbeat_marks_ownership_lost_and_stops(self):
        job_service = MagicMock()
        job_service.heartbeat_job.return_value = False
        heartbeat = worker.GoogleReviewSyncHeartbeat(
            job_service,
            {"id": 41, "business_id": 9},
            "worker-a",
            0.01,
            120,
        )

        heartbeat.start()
        deadline = time.monotonic() + 1
        while not heartbeat.ownership_lost and time.monotonic() < deadline:
            time.sleep(0.01)
        heartbeat.stop()

        self.assertTrue(heartbeat.ownership_lost)
        self.assertEqual(1, job_service.heartbeat_job.call_count)


if __name__ == "__main__":
    unittest.main()
