import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIVE_TEMPLATE = ROOT / "app" / "templates" / "live_dashboard.html"
CONSULTANT_TEMPLATE = ROOT / "app" / "templates" / "ai_consultant.html"
HEADER_TEMPLATE = ROOT / "app" / "templates" / "components" / "business_workspace_header.html"
WORKSPACE_CSS = ROOT / "app" / "static" / "css" / "business_workspace.css"


class BusinessWorkspaceShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live = LIVE_TEMPLATE.read_text(encoding="utf-8")
        cls.consultant = CONSULTANT_TEMPLATE.read_text(encoding="utf-8")
        cls.header = HEADER_TEMPLATE.read_text(encoding="utf-8")
        cls.css = WORKSPACE_CSS.read_text(encoding="utf-8")

    def test_all_tabs_keep_their_server_rendered_endpoints(self):
        self.assertIn("google_business.live_dashboard", self.header)
        self.assertIn("tab='reviews'", self.header)
        self.assertIn("tab='performance'", self.header)
        self.assertIn("ai_consultant.ai_consultant_page", self.header)
        for label in ("Live Reviews", "Performance Analysis", "AI Business Consultant"):
            self.assertIn(label, self.header)

    def test_active_tab_is_accessibly_identified(self):
        self.assertIn('aria-current="page"', self.header)
        self.assertIn('aria-label="Business workspace sections"', self.header)
        self.assertIn('active_tab,', self.live)
        self.assertIn('"consultant",', self.consultant)

    def test_review_sync_keeps_original_queue_contract(self):
        self.assertIn("enqueue_google_review_sync_job", self.live)
        self.assertIn("active_google_review_sync_job", self.live)
        self.assertIn("data-google-review-sync-button", self.live)
        self.assertNotIn('action="/businesses/{{ business_id }}/google/sync-reviews"', self.live)

    def test_performance_sync_keeps_post_dates_and_csrf(self):
        self.assertIn('method="POST" action="/businesses/{{ business_id }}/google/sync-performance"', self.live)
        self.assertIn('name="start_date"', self.live)
        self.assertIn('name="end_date"', self.live)
        self.assertIn("{{ csrf_field() }}", self.live)

    def test_select_location_and_disconnected_flow_remain(self):
        self.assertIn("/businesses/{{ business_id }}/google/select-location", self.live)
        self.assertIn("Connect Google Business Profile", self.live)
        self.assertIn('{% include "_google_oauth_notice.html" %}', self.live)

    def test_consultant_primary_action_keeps_post_and_csrf(self):
        action = 'method="POST" action="/business/{{ business_id }}/ai-consultant/generate"'
        self.assertIn(action, self.consultant)
        self.assertIn('name="period" value="{{ selected_period }}"', self.consultant)
        self.assertIn("Refresh analysis", self.consultant)
        self.assertIn("{{ csrf_field() }}", self.consultant)

    def test_shared_shell_supports_theme_mobile_and_reduced_motion(self):
        self.assertIn("var(--rs-surface)", self.css)
        self.assertIn('[data-theme="dark"]', self.css)
        self.assertIn("overflow-x: auto", self.css)
        self.assertIn("@media (max-width: 767.98px)", self.css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.css)


if __name__ == "__main__":
    unittest.main()
