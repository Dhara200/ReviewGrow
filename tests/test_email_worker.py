import unittest
from unittest.mock import patch

import worker


class EmailWorkerTests(unittest.TestCase):
    def setUp(self):
        worker.shutdown_requested = False
        worker.shutdown_event.clear()

    @patch("worker.mark_email_sent", return_value=True)
    @patch("worker.send_queued_email", return_value="ses-1")
    @patch("worker.claim_pending_email_jobs")
    @patch("worker.claim_next_job", return_value=None)
    @patch("worker.google_review_sync_jobs")
    def test_claimed_job_is_not_processed_twice(self, google, _ai, claim, send, _sent):
        google.get_oldest_pending_job.return_value = None
        job = {"id": 3, "email_type": "welcome", "recipient_email": "a@example.com"}
        claim.side_effect = [[job], []]
        worker.run_worker_iteration()
        worker.run_worker_iteration()
        send.assert_called_once_with(job)


if __name__ == "__main__":
    unittest.main()
