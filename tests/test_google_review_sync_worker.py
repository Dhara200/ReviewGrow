import unittest
from unittest.mock import patch

import worker


class GoogleReviewSyncWorkerTests(unittest.TestCase):
    @patch("worker.process_analysis_job")
    @patch("worker.claim_next_job", return_value=None)
    @patch("worker._process_google_review_sync_job")
    @patch("worker.google_review_sync_jobs")
    def test_claimed_sync_job_is_processed(
        self,
        job_service,
        process_sync,
        _claim_analysis,
        _process_analysis,
    ):
        job = {"id": 41, "user_id": 7, "business_id": 9}
        job_service.get_oldest_pending_job.return_value = job
        job_service.claim_job.return_value = True

        processed = worker.run_worker_iteration()

        self.assertTrue(processed)
        job_service.claim_job.assert_called_once_with(41)
        process_sync.assert_called_once_with(job)

    @patch("worker.process_analysis_job")
    @patch("worker.claim_next_job", return_value=None)
    @patch("worker._process_google_review_sync_job")
    @patch("worker.google_review_sync_jobs")
    def test_job_is_not_processed_when_atomic_claim_loses(
        self,
        job_service,
        process_sync,
        _claim_analysis,
        _process_analysis,
    ):
        job_service.get_oldest_pending_job.return_value = {"id": 41}
        job_service.claim_job.return_value = False

        processed = worker.run_worker_iteration()

        self.assertFalse(processed)
        process_sync.assert_not_called()

    @patch("worker.run_google_review_sync")
    @patch("worker.google_review_sync_jobs")
    def test_success_records_counts_and_completes_job(self, job_service, run_sync):
        run_sync.return_value = {
            "fetched_count": 12,
            "inserted_count": 5,
            "updated_count": 2,
        }
        job = {"id": 41, "user_id": 7, "business_id": 9}

        worker._process_google_review_sync_job(job)

        run_sync.assert_called_once_with(7, 9)
        fields = job_service.update_job.call_args.kwargs
        self.assertEqual("completed", fields["status"])
        self.assertEqual(12, fields["fetched_count"])
        self.assertEqual(5, fields["inserted_count"])
        self.assertEqual(2, fields["updated_count"])
        self.assertIsNone(fields["error_message"])

    @patch("worker.logger")
    @patch("worker.run_google_review_sync")
    @patch("worker.google_review_sync_jobs")
    def test_failure_is_sanitized_and_marks_job_failed(
        self,
        job_service,
        run_sync,
        logger,
    ):
        run_sync.side_effect = RuntimeError(
            "Authorization: Bearer secret-token refresh_token=refresh-secret failed"
        )
        job = {"id": 41, "user_id": 7, "business_id": 9}

        worker._process_google_review_sync_job(job)

        fields = job_service.update_job.call_args.kwargs
        self.assertEqual("failed", fields["status"])
        self.assertIn("[REDACTED]", fields["error_message"])
        self.assertNotIn("secret-token", fields["error_message"])
        self.assertNotIn("refresh-secret", fields["error_message"])
        logger.error.assert_called_once()

    @patch("worker.process_analysis_job")
    @patch("worker.claim_next_job", return_value={"id": 88})
    @patch("worker.logger")
    @patch("worker.run_google_review_sync", side_effect=RuntimeError("temporary failure"))
    @patch("worker.google_review_sync_jobs")
    def test_failed_sync_boundary_does_not_prevent_analysis_progress(
        self,
        job_service,
        _run_sync,
        _logger,
        _claim_analysis,
        process_analysis,
    ):
        job_service.get_oldest_pending_job.return_value = {
            "id": 41,
            "user_id": 7,
            "business_id": 9,
        }
        job_service.claim_job.return_value = True

        processed = worker.run_worker_iteration()

        self.assertTrue(processed)
        self.assertEqual("failed", job_service.update_job.call_args.kwargs["status"])
        process_analysis.assert_called_once_with(88, batch_size=worker.Config.AI_BATCH_SIZE)


if __name__ == "__main__":
    unittest.main()
