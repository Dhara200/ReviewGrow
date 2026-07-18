import unittest
from pathlib import Path


TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "app" / "templates" / "live_dashboard.html"


class GoogleReviewSyncUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = TEMPLATE_PATH.read_text(encoding="utf-8")

    def test_sync_controls_use_enqueue_endpoint_not_synchronous_route(self):
        self.assertIn("data-google-review-sync-button", self.template)
        self.assertEqual(3, self.template.count("data-google-review-sync-button>"))
        self.assertIn("enqueue_google_review_sync_job", self.template)
        self.assertNotIn('action="/businesses/{{ business_id }}/google/sync-reviews"', self.template)

    def test_enqueue_post_includes_csrf_header(self):
        self.assertIn('"X-CSRF-Token": window.reviewGrowCsrfToken', self.template)

    def test_status_updates_are_accessible(self):
        self.assertIn('data-google-review-sync-status aria-live="polite"', self.template)

    def test_polling_interval_and_timeout_are_named(self):
        self.assertIn("const REVIEW_SYNC_POLL_INTERVAL_MS = 2000", self.template)
        self.assertIn("const REVIEW_SYNC_POLL_TIMEOUT_MS = 10 * 60 * 1000", self.template)

    def test_pending_processing_and_terminal_states_are_handled(self):
        for expected in (
            "Synchronization queued…", "Synchronizing Google reviews…",
            'data.status === "completed"', 'data.status === "failed"',
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, self.template)

    def test_completed_state_displays_counts_and_reloads(self):
        self.assertIn("data.fetched_count", self.template)
        self.assertIn("data.inserted_count", self.template)
        self.assertIn("data.updated_count", self.template)
        self.assertIn("window.location.reload()", self.template)

    def test_failed_state_reenables_button(self):
        self.assertIn("data.error_message ||", self.template)
        self.assertIn('{ disabled: false, label: "Sync Reviews", error: true }', self.template)

    def test_temporary_poll_error_is_tolerated(self):
        self.assertIn("Synchronization is continuing. Checking status again…", self.template)
        self.assertIn("scheduleReviewSyncPoll();", self.template)

    def test_timeout_does_not_fail_or_cancel_backend_job(self):
        self.assertIn(
            "Synchronization is taking longer than expected and may still be running in the background.",
            self.template,
        )
        self.assertNotIn("cancel-review-sync", self.template)

    def test_duplicate_clicks_and_polling_loops_are_guarded(self):
        self.assertIn("if (enqueueInProgress || activeReviewSyncJobId)", self.template)
        self.assertIn("if (activeReviewSyncJobId === jobId && reviewSyncTimer !== null)", self.template)

    def test_active_job_is_discovered_without_enqueue(self):
        self.assertIn("active_google_review_sync_job", self.template)
        self.assertIn("resumeActiveReviewSync();", self.template)

    def test_polling_stops_when_page_unloads(self):
        self.assertIn('window.addEventListener("beforeunload", stopReviewSyncPolling', self.template)


if __name__ == "__main__":
    unittest.main()
