import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from app.routes.reviews import review_bp
from app.services.business_analytics_service import get_google_review_snapshot


class FakeHistoryCursor:
    def __init__(self, total=60):
        self.total = total
        self.executions = []
        self.rows = []
        self.row = None

    def execute(self, query, params=()):
        normalized = " ".join(query.split())
        self.executions.append((normalized, tuple(params)))
        if normalized.startswith("SELECT COUNT(*) AS total_count"):
            self.row = {"total_count": self.total}
        elif "SELECT id, source, rating" in normalized:
            per_page, offset = params[-2:]
            available = max(min(per_page, self.total - offset), 0)
            self.rows = [
                {
                    "id": self.total - offset - index,
                    "source": "google",
                    "rating": 5,
                    "review_title": None,
                    "review_text": f"review {offset + index}",
                    "reviewer_name": "Reviewer",
                    "review_date": None,
                    "analysis_status": "pending",
                    "sentiment": None,
                    "summary": None,
                    "ai_reply": None,
                    "analyzed_at": None,
                }
                for index in range(available)
            ]
        elif "SUM(analysis_status='pending')" in normalized:
            self.row = {
                "total_count": self.total,
                "pending_count": self.total,
                "analyzed_count": 0,
                "rating_5": self.total,
                "rating_4": 0, "rating_3": 0, "rating_2": 0, "rating_1": 0,
            }
        elif "GROUP BY COALESCE(source" in normalized:
            self.rows = [{"source": "google", "source_count": self.total}]

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, dictionary=False):
        return self._cursor

    def close(self):
        pass


class ReviewHistoryPaginationTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY="pagination-test")
        self.app.register_blueprint(review_bp)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = 7
            session["role"] = "admin"

    def request_page(self, query="", total=60):
        cursor = FakeHistoryCursor(total)
        with patch("app.routes.reviews.user_owns_business", return_value=True), \
             patch("app.routes.reviews.get_connection", return_value=FakeConnection(cursor)), \
             patch("app.routes.reviews.render_template", side_effect=lambda _name, **ctx: ctx):
            response = self.client.get(f"/reviews/history/9{query}")
        self.assertEqual(200, response.status_code)
        return response.get_json(), cursor

    def test_default_page_size_total_and_stable_order(self):
        context, cursor = self.request_page()
        self.assertEqual(25, len(context["reviews"]))
        self.assertEqual(60, context["pagination"]["total"])
        listing = next(query for query, _ in cursor.executions if "LIMIT %s OFFSET %s" in query)
        self.assertIn("id DESC", listing)

    def test_second_page_has_no_duplicates_or_missing_rows(self):
        first, _ = self.request_page("?page=1")
        second, _ = self.request_page("?page=2")
        first_ids = [row["id"] for row in first["reviews"]]
        second_ids = [row["id"] for row in second["reviews"]]
        self.assertFalse(set(first_ids) & set(second_ids))
        self.assertEqual(first_ids[-1] - 1, second_ids[0])

    def test_filters_and_tenant_scope_are_parameterized_and_preserved(self):
        context, cursor = self.request_page("?page=2&rating=5&source=google&search=great")
        count_query, params = cursor.executions[0]
        self.assertIn("business_id=%s", count_query)
        self.assertIn("source=%s", count_query)
        self.assertEqual(9, params[0])
        self.assertIn("rating=5", context["pagination"]["next_url"])
        self.assertIn("source=google", context["pagination"]["next_url"])
        self.assertIn("search=great", context["pagination"]["next_url"])

    def test_invalid_inputs_maximum_page_size_and_empty_results(self):
        invalid, _ = self.request_page("?page=bad&per_page=999")
        self.assertEqual(1, invalid["pagination"]["page"])
        self.assertEqual(25, invalid["pagination"]["per_page"])
        maximum, _ = self.request_page("?per_page=100", total=150)
        self.assertEqual(100, len(maximum["reviews"]))
        empty, _ = self.request_page("?page=99", total=10)
        self.assertEqual([], empty["reviews"])
        self.assertEqual(10, empty["pagination"]["total"])

    def test_tenant_isolation_rejects_unowned_business(self):
        with patch("app.routes.reviews.user_owns_business", return_value=False), \
             patch("app.routes.reviews.get_connection") as connection:
            response = self.client.get("/reviews/history/999?page=2")
        self.assertEqual(403, response.status_code)
        connection.assert_not_called()

    def test_templates_include_accessible_pagination_controls(self):
        root = Path(__file__).resolve().parents[1] / "app" / "templates"
        for name in ("review_history.html", "live_dashboard.html"):
            template = (root / name).read_text(encoding="utf-8")
            self.assertIn('aria-label="', template)
            self.assertIn("Showing {{", template)
            self.assertIn(">Previous<", template)
            self.assertIn(">Next<", template)


class SnapshotCursor:
    def __init__(self):
        self.executions = []
        self.rows = []
        self.row = None

    def execute(self, query, params=()):
        normalized = " ".join(query.split())
        self.executions.append((normalized, tuple(params)))
        if "SELECT COUNT(*) AS filtered_count" in normalized:
            self.row = {"filtered_count": 51}
        elif "five_star_reviews" in normalized:
            self.row = {"total_reviews": 51}
        elif "LIMIT 5" in normalized:
            self.rows = []
        else:
            self.rows = [{"id": 26}]

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class GoogleSnapshotPaginationTests(unittest.TestCase):
    @patch("app.services.business_analytics_service.calculate_business_metrics")
    @patch("app.services.business_analytics_service.get_connection")
    def test_limit_offset_count_filters_and_stable_order(self, connection, metrics):
        cursor = SnapshotCursor()
        connection.return_value = FakeConnection(cursor)
        metrics.return_value = {
            "total_reviews": 51, "average_rating": 4,
            "positive_review_count": 40, "neutral_review_count": 5,
            "negative_review_count": 6,
        }
        _stats, reviews, summary, _urgent = get_google_review_snapshot(
            9, "location-1", limit=25, offset=25, page=2,
            filters={"rating": "5", "search": "great"},
        )
        listing, params = cursor.executions[0]
        self.assertIn("r.business_id=%s", listing)
        self.assertIn("r.id DESC", listing)
        self.assertIn("LIMIT %s OFFSET %s", listing)
        self.assertEqual((25, 25), params[-2:])
        self.assertEqual([{"id": 26}], reviews)
        self.assertEqual(51, summary["filtered_reviews"])
        self.assertEqual(3, summary["total_pages"])


if __name__ == "__main__":
    unittest.main()
