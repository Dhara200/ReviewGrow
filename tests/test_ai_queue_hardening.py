import unittest
from unittest.mock import patch

from flask import Flask

from app.routes.ai_consultant_routes import ai_consultant_bp
from app.services.analysis_job_service import claim_next_job


class Cursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.executions = []
        self.rowcount = 1

    def execute(self, query, params=()):
        self.executions.append((" ".join(query.split()), tuple(params)))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def close(self):
        pass


class Connection:
    def __init__(self, cursor): self._cursor = cursor
    def cursor(self, dictionary=False): return self._cursor
    def start_transaction(self): pass
    def commit(self): pass
    def rollback(self): pass
    def close(self): pass


class AIQueueHardeningTests(unittest.TestCase):
    def test_claim_is_atomic_due_and_lease_guarded(self):
        cursor = Cursor([{"id": 41, "status": "pending"}])
        with patch(
            "app.services.analysis_job_service.get_connection",
            return_value=Connection(cursor),
        ):
            job = claim_next_job("worker-a", 120)
        self.assertEqual(41, job["id"])
        self.assertIn("FOR UPDATE SKIP LOCKED", cursor.executions[0][0])
        self.assertIn("next_attempt_at", cursor.executions[0][0])
        self.assertIn("worker_id=%s", cursor.executions[1][0])
        self.assertIn("attempt_count=attempt_count+1", cursor.executions[1][0])

    def test_consultant_route_only_enqueues(self):
        app = Flask(__name__)
        app.config.update(TESTING=True, SECRET_KEY="ai-queue-test")
        app.register_blueprint(ai_consultant_bp)
        client = app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = 7
            session["role"] = "admin"
        with patch("app.routes.ai_consultant_routes.user_owns_business", return_value=True), \
             patch("app.routes.ai_consultant_routes._load_google_connection", return_value={"google_location_id": "loc-1"}), \
             patch("app.routes.ai_consultant_routes.get_business_review_metrics", return_value={"total_reviews": 8}), \
             patch("app.routes.ai_consultant_routes.create_consultant_job", return_value=(55, True)), \
             patch("app.services.ai_consultant_service.AIService") as provider:
            response = client.post("/business/9/ai-consultant/generate")
        self.assertEqual(302, response.status_code)
        self.assertTrue(response.location.endswith("/business/9/ai-consultant?job=55"))
        provider.assert_not_called()


if __name__ == "__main__":
    unittest.main()
