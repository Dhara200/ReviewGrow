import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "app" / "templates" / "live_dashboard.html").read_text(encoding="utf-8")
CSS = (ROOT / "app" / "static" / "css" / "live_dashboard.css").read_text(encoding="utf-8")


class LiveReviewsPhase2Tests(unittest.TestCase):
    def test_verified_kpis_are_used(self):
        for field in (
            "stats.average_rating",
            "stats.total_reviews",
            "review_summary.unanswered_reviews",
            "connection.last_sync_at",
        ):
            self.assertIn(field, TEMPLATE)
        for label in ("Average Rating", "Total Reviews", "Awaiting Reply", "Last Sync"):
            self.assertIn(label, TEMPLATE)

    def test_distinct_empty_states_exist(self):
        for message in (
            "Select your Google location",
            "Sync your reviews for the first time",
            "No Google reviews yet",
            "No reviews match these filters",
            "Google Business Profile is not connected",
        ):
            self.assertIn(message, TEMPLATE)

    def test_sync_queue_contract_is_unchanged(self):
        self.assertEqual(2, TEMPLATE.count("data-google-review-sync-button>"))
        self.assertIn("enqueue_google_review_sync_job", TEMPLATE)
        self.assertIn("active_google_review_sync_job", TEMPLATE)
        self.assertIn("const REVIEW_SYNC_POLL_INTERVAL_MS = 2000", TEMPLATE)
        self.assertIn("const REVIEW_SYNC_POLL_TIMEOUT_MS = 10 * 60 * 1000", TEMPLATE)

    def test_reply_actions_and_selectors_remain(self):
        for selector in (
            "data-regenerate-reply",
            "data-approve-reply",
            "data-post-reply",
            "data-suggested-reply",
            "data-reply-review-id",
        ):
            self.assertIn(selector, TEMPLATE)
        for label in ("Generate AI Reply", "Save Reply", "Post Reply"):
            self.assertIn(label, TEMPLATE)

    def test_filters_and_pagination_fields_are_preserved(self):
        for name in ("rating", "sentiment", "reply_status", "period", "search", "date_from", "date_to", "per_page"):
            self.assertIn(f'name="{name}"', TEMPLATE)
        self.assertIn("review_pagination.previous_url", TEMPLATE)
        self.assertIn("review_pagination.next_url", TEMPLATE)
        self.assertIn("review_pagination.page_size_urls", TEMPLATE)

    def test_reply_settings_post_and_csrf_remain(self):
        self.assertIn('action="/businesses/{{ business_id }}/reply-settings"', TEMPLATE)
        self.assertIn("{{ csrf_field() }}", TEMPLATE)
        self.assertIn("auto_generate_replies_for_new_reviews", TEMPLATE)
        self.assertIn("auto_post_replies", TEMPLATE)

    def test_card_layout_is_responsive_and_theme_safe(self):
        self.assertIn(".live-kpi-grid", CSS)
        self.assertIn(".live-feed-card tbody tr", CSS)
        self.assertIn("var(--rs-surface)", CSS)
        self.assertIn("@media (max-width: 767.98px)", CSS)
        self.assertIn("@media (max-width: 439.98px)", CSS)
        self.assertIn("@media (prefers-reduced-motion: reduce)", CSS)


if __name__ == "__main__":
    unittest.main()
