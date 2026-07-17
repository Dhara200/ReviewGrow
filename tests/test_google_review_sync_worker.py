import unittest
from unittest.mock import patch

import requests

import worker
from app.services.google_business_service import GoogleBusinessError, GoogleTransientError


class GoogleReviewSyncWorkerTests(unittest.TestCase):
    def setUp(self):
        worker.shutdown_requested = False
        worker.shutdown_event.clear()

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

    @patch("worker.perform_google_review_post_sync")
    @patch("worker.run_google_review_sync")
    @patch("worker.google_review_sync_jobs")
    def test_success_runs_post_sync_once_and_completes_job(
        self, job_service, run_sync, post_sync
    ):
        run_sync.return_value = {
            "fetched_count": 12,
            "inserted_count": 5,
            "updated_count": 2,
        }
        job = {"id": 41, "user_id": 7, "business_id": 9}

        worker._process_google_review_sync_job(job)

        run_sync.assert_called_once_with(7, 9)
        post_sync.assert_called_once_with(7, 9, run_sync.return_value, None)
        fields = job_service.update_job.call_args.kwargs
        self.assertEqual("completed", fields["status"])
        self.assertEqual(12, fields["fetched_count"])
        self.assertEqual(5, fields["inserted_count"])
        self.assertEqual(2, fields["updated_count"])
        self.assertIsNone(fields["error_message"])

    @patch("worker.logger")
    @patch("worker.perform_google_review_post_sync", side_effect=RuntimeError("Bearer secret"))
    @patch("worker.run_google_review_sync")
    @patch("worker.google_review_sync_jobs")
    def test_post_sync_failure_is_sanitized_and_marks_job_failed(
        self, job_service, run_sync, post_sync, logger
    ):
        run_sync.return_value = {
            "fetched_count": 2,
            "inserted_count": 1,
            "updated_count": 0,
            "google_location_id": "locations/2",
        }

        worker._process_google_review_sync_job({"id": 41, "user_id": 7, "business_id": 9})

        post_sync.assert_called_once()
        updates = job_service.update_job.call_args_list
        self.assertEqual(1, len(updates))
        self.assertEqual("failed", updates[0].kwargs["status"])
        self.assertNotIn("secret", updates[0].kwargs["error_message"])
        logger.error.assert_called_once()

    @patch("worker.logger")
    @patch("worker.run_google_review_sync", side_effect=RuntimeError("sync failed"))
    @patch("worker.google_review_sync_jobs")
    def test_failed_status_persistence_failure_does_not_escape(
        self, job_service, _run_sync, logger
    ):
        job_service.update_job.side_effect = RuntimeError("password=secret unavailable")

        worker._process_google_review_sync_job({"id": 41, "user_id": 7, "business_id": 9})

        self.assertEqual(1, job_service.update_job.call_count)
        self.assertEqual(2, logger.error.call_count)

    @patch("worker.logger")
    @patch("worker.perform_google_review_post_sync")
    @patch("worker.run_google_review_sync")
    @patch("worker.google_review_sync_jobs")
    def test_completed_status_persistence_failure_does_not_rerun_or_mark_failed(
        self, job_service, run_sync, _post_sync, logger
    ):
        run_sync.return_value = {
            "fetched_count": 2,
            "inserted_count": 1,
            "updated_count": 0,
        }
        job_service.update_job.side_effect = RuntimeError("database unavailable")

        worker._process_google_review_sync_job({"id": 41, "user_id": 7, "business_id": 9})

        run_sync.assert_called_once_with(7, 9)
        job_service.update_job.assert_called_once()
        self.assertEqual("completed", job_service.update_job.call_args.kwargs["status"])
        logger.error.assert_called_once()

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

    @patch("worker.run_google_review_sync")
    def test_retryable_failure_retries_then_succeeds(self, run_sync):
        run_sync.side_effect = [
            requests.Timeout("temporary timeout"),
            {"fetched_count": 1, "inserted_count": 1, "updated_count": 0},
        ]
        delays = []

        result = worker._run_google_review_sync_with_retries(
            {"id": 41, "user_id": 7, "business_id": 9},
            sleep=delays.append,
            jitter=lambda _low, _high: 0.25,
        )

        self.assertEqual(1, result["fetched_count"])
        self.assertEqual([2.25], delays)
        self.assertEqual(2, run_sync.call_count)

    @patch("worker.run_google_review_sync")
    def test_non_retryable_failure_is_not_retried(self, run_sync):
        run_sync.side_effect = GoogleBusinessError("Location is invalid.")
        delays = []

        with self.assertRaises(GoogleBusinessError):
            worker._run_google_review_sync_with_retries(
                {"id": 41, "user_id": 7, "business_id": 9},
                sleep=delays.append,
            )

        self.assertEqual(1, run_sync.call_count)
        self.assertEqual([], delays)

    @patch("worker.run_google_review_sync")
    def test_retry_limit_has_no_sleep_after_final_failure(self, run_sync):
        run_sync.side_effect = GoogleTransientError("Google is unavailable.")
        delays = []

        with patch.object(worker.Config, "GOOGLE_REVIEW_SYNC_MAX_RETRIES", 3):
            with self.assertRaises(GoogleTransientError):
                worker._run_google_review_sync_with_retries(
                    {"id": 41, "user_id": 7, "business_id": 9},
                    sleep=delays.append,
                    jitter=lambda _low, _high: 0,
                )

        self.assertEqual(4, run_sync.call_count)
        self.assertEqual([2, 4, 8], delays)

    def test_backoff_calculation_uses_exponential_delay_and_jitter(self):
        with patch.object(worker.Config, "GOOGLE_REVIEW_SYNC_BACKOFF_BASE_SECONDS", 2):
            with patch.object(worker.Config, "GOOGLE_REVIEW_SYNC_BACKOFF_JITTER_SECONDS", 0.5):
                delays = [
                    worker._retry_backoff_seconds(number, jitter=lambda _low, _high: 0.1)
                    for number in (1, 2, 3)
                ]

        self.assertEqual([2.1, 4.1, 8.1], delays)

    def test_worker_error_backoff_config_is_non_negative(self):
        self.assertGreaterEqual(worker.Config.WORKER_ERROR_BACKOFF_SECONDS, 0)

    def test_infrastructure_error_sanitizer_redacts_database_credentials(self):
        message = worker._safe_error_message(
            RuntimeError(
                "password=secret dsn=mysql://user:pass@database/reviewgrow "
                "Authorization: Bearer oauth-token"
            )
        )

        self.assertNotIn("secret", message)
        self.assertNotIn("user:pass", message)
        self.assertNotIn("oauth-token", message)

    @patch("worker.run_worker_iteration", return_value=False)
    @patch("worker.reset_stale_processing_jobs")
    @patch("worker._wait_for_shutdown")
    @patch("worker.google_review_sync_jobs")
    def test_startup_recovery_runs_once(self, job_service, wait, reset_ai, iteration):
        job_service.recover_stale_processing_jobs.return_value = 2
        wait.side_effect = lambda _seconds: setattr(worker, "shutdown_requested", True) or True

        worker.run_worker_forever()

        job_service.recover_stale_processing_jobs.assert_called_once_with(
            worker.Config.GOOGLE_REVIEW_SYNC_STALE_TIMEOUT_MINUTES
        )
        reset_ai.assert_called_once()
        iteration.assert_called_once()

    @patch("worker.logger")
    @patch("worker.run_worker_iteration", return_value=False)
    @patch("worker.reset_stale_processing_jobs")
    @patch("worker._wait_for_shutdown")
    @patch("worker.google_review_sync_jobs")
    def test_startup_recovery_retries_after_failure_then_polls(
        self, job_service, wait, reset_ai, iteration, logger
    ):
        job_service.recover_stale_processing_jobs.side_effect = [
            RuntimeError("database unavailable"),
            3,
        ]

        def wait_side_effect(_seconds):
            if job_service.recover_stale_processing_jobs.call_count == 2:
                worker.shutdown_requested = True
                return True
            return False

        wait.side_effect = wait_side_effect

        worker.run_worker_forever()

        self.assertEqual(2, job_service.recover_stale_processing_jobs.call_count)
        reset_ai.assert_called_once()
        iteration.assert_called_once()
        self.assertTrue(logger.error.called)

    @patch("worker.logger")
    @patch("worker._wait_for_shutdown")
    @patch("worker.google_review_sync_jobs")
    def test_google_discovery_and_claim_failures_do_not_terminate_worker(
        self, job_service, wait, logger
    ):
        for failure_point in ("discovery", "claim"):
            with self.subTest(failure_point=failure_point):
                worker.shutdown_requested = False
                job_service.reset_mock()
                wait.reset_mock()
                job_service.recover_stale_processing_jobs.return_value = 0
                if failure_point == "discovery":
                    job_service.get_oldest_pending_job.side_effect = RuntimeError("db down")
                else:
                    job_service.get_oldest_pending_job.side_effect = None
                    job_service.get_oldest_pending_job.return_value = {"id": 41}
                    job_service.claim_job.side_effect = RuntimeError("db down")
                wait.side_effect = lambda _seconds: setattr(
                    worker, "shutdown_requested", True
                ) or True

                worker.run_worker_forever()

                self.assertTrue(logger.error.called)
                wait.assert_called_with(worker.Config.WORKER_ERROR_BACKOFF_SECONDS)

    @patch("worker.logger")
    @patch("worker.claim_next_job", side_effect=RuntimeError("db down"))
    @patch("worker.reset_stale_processing_jobs")
    @patch("worker._wait_for_shutdown")
    @patch("worker.google_review_sync_jobs")
    def test_ai_discovery_failure_does_not_terminate_worker(
        self, job_service, wait, _reset_ai, _claim_ai, logger
    ):
        job_service.recover_stale_processing_jobs.return_value = 0
        job_service.get_oldest_pending_job.return_value = None
        wait.side_effect = lambda _seconds: setattr(worker, "shutdown_requested", True) or True

        worker.run_worker_forever()

        self.assertTrue(logger.error.called)
        wait.assert_called_once_with(worker.Config.WORKER_ERROR_BACKOFF_SECONDS)

    @patch("worker.run_worker_iteration", return_value=False)
    @patch("worker.reset_stale_processing_jobs")
    @patch("worker._wait_for_shutdown")
    @patch("worker.google_review_sync_jobs")
    def test_empty_poll_uses_normal_delay_not_error_backoff(
        self, job_service, wait, _reset_ai, _iteration
    ):
        job_service.recover_stale_processing_jobs.return_value = 0
        wait.side_effect = lambda _seconds: setattr(worker, "shutdown_requested", True) or True

        worker.run_worker_forever()

        wait.assert_called_once_with(worker.Config.AI_WORKER_POLL_SECONDS)

    @patch("worker.logger")
    @patch("worker._wait_for_shutdown", return_value=True)
    @patch("worker.google_review_sync_jobs")
    def test_shutdown_interrupts_startup_recovery_backoff(self, job_service, wait, logger):
        job_service.recover_stale_processing_jobs.side_effect = RuntimeError("db down")

        worker.run_worker_forever()

        job_service.recover_stale_processing_jobs.assert_called_once()
        wait.assert_called_once_with(worker.Config.WORKER_ERROR_BACKOFF_SECONDS)
        self.assertTrue(logger.info.called)

    def test_shutdown_handler_requests_exit_without_interrupting_current_work(self):
        worker._request_shutdown(15, None)

        self.assertTrue(worker.shutdown_requested)
        self.assertTrue(worker.shutdown_event.is_set())

    @patch("worker.claim_next_job")
    @patch("worker._process_google_review_sync_job")
    @patch("worker.google_review_sync_jobs")
    def test_shutdown_during_google_job_does_not_start_new_ai_job(
        self, job_service, process_sync, claim_analysis
    ):
        job = {"id": 41, "user_id": 7, "business_id": 9}
        job_service.get_oldest_pending_job.return_value = job
        job_service.claim_job.return_value = True
        process_sync.side_effect = lambda _job: setattr(worker, "shutdown_requested", True)

        worker.run_worker_iteration()

        process_sync.assert_called_once_with(job)
        claim_analysis.assert_not_called()

    @patch("worker.process_analysis_job")
    @patch("worker.claim_next_job", return_value={"id": 88})
    @patch("worker._process_google_review_sync_job")
    @patch("worker.google_review_sync_jobs")
    def test_google_and_ai_queues_both_progress(
        self, job_service, process_sync, _claim_analysis, process_analysis
    ):
        job = {"id": 41, "user_id": 7, "business_id": 9}
        job_service.get_oldest_pending_job.return_value = job
        job_service.claim_job.return_value = True

        worker.run_worker_iteration()

        process_sync.assert_called_once_with(job)
        process_analysis.assert_called_once_with(88, batch_size=worker.Config.AI_BATCH_SIZE)


if __name__ == "__main__":
    unittest.main()
