import ast
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.services.google_review_post_sync_service import perform_google_review_post_sync


class GoogleReviewPostSyncServiceTests(unittest.TestCase):
    @patch("app.services.google_review_post_sync_service.create_analysis_job")
    @patch("app.services.google_review_post_sync_service.refresh_business_review_analytics")
    def test_changed_reviews_refresh_analytics_then_enqueue_analysis(self, refresh, create):
        calls = []
        refresh.side_effect = lambda *args, **kwargs: (
            calls.append("analytics") or {"topic_result": {"inserted_topics": 3}}
        )
        create.side_effect = lambda *args, **kwargs: calls.append("analysis") or (18, True)

        result = perform_google_review_post_sync(
            7, 9, {"inserted_count": 2, "updated_count": 1}, "locations/2"
        )

        self.assertEqual(["analytics", "analysis"], calls)
        refresh.assert_called_once_with(
            9,
            mark_consultant_outdated=True,
            source="google",
            google_location_id="locations/2",
            require_google_review_id=True,
        )
        create.assert_called_once_with(7, 9, force_reanalysis=False)
        self.assertEqual(18, result["analysis_job_id"])

    @patch("app.services.google_review_post_sync_service.create_analysis_job", return_value=(18, False))
    @patch("app.services.google_review_post_sync_service.refresh_business_review_analytics")
    def test_unchanged_reviews_skip_analytics_but_still_enqueue_analysis(self, refresh, _create):
        perform_google_review_post_sync(
            7, 9, {"inserted_count": 0, "updated_count": 0}, "locations/2"
        )
        refresh.assert_not_called()

    @patch("app.services.google_review_post_sync_service.create_analysis_job")
    @patch(
        "app.services.google_review_post_sync_service.refresh_business_review_analytics",
        side_effect=RuntimeError("metrics failed"),
    )
    def test_analytics_failure_propagates_and_stops_later_work(self, _refresh, create):
        with self.assertRaisesRegex(RuntimeError, "metrics failed"):
            perform_google_review_post_sync(
                7, 9, {"inserted_count": 1, "updated_count": 0}, "locations/2"
            )
        create.assert_not_called()

    def test_service_has_no_flask_imports(self):
        source_path = Path("app/services/google_review_post_sync_service.py")
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imported_roots = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertNotIn("flask", imported_roots)


if __name__ == "__main__":
    unittest.main()
