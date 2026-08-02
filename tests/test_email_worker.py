import unittest
from unittest.mock import patch

import worker


class EmailWorkerTests(unittest.TestCase):
    def setUp(self):
        worker.shutdown_requested = False
        worker.shutdown_event.clear()

    @patch("worker.mark_email_sent", return_value=True)
    @patch("worker.send_queued_email", return_value="ses-1")
    @patch("worker.is_login_otp_challenge_active", return_value=True)
    @patch("worker.claim_pending_email_jobs")
    @patch("worker.claim_next_job", return_value=None)
    @patch("worker.google_review_sync_jobs")
    def test_claimed_job_is_not_processed_twice(self, google, _ai, claim, _active, send, _sent):
        google.get_oldest_pending_job.return_value = None
        job = {"id": 3, "email_type": "login_otp", "recipient_email": "a@example.com",
               "template_data": {"challenge_id": 8}}
        claim.side_effect = [[job], []]
        worker.run_worker_iteration()
        worker.run_worker_iteration()
        send.assert_called_once_with(job)


if __name__ == "__main__":
    unittest.main()
