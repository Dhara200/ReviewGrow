import copy
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from flask import Flask

import worker
from app.routes.google_business import google_business_bp
from app.services.csrf_service import CSRF_SESSION_KEY, init_csrf
from app.services.google_business_service import GoogleTransientError
from app.services.review_sync_service import sync_google_reviews


class InMemoryGoogleJobStore:
    def __init__(self):
        self.jobs = {}
        self.next_id = 1
        self.claim_snapshots = []

    def create_job(self, user_id, business_id):
        active = self.get_active_job(business_id, user_id)
        if active:
            return active["id"], False
        job_id = self.next_id
        self.next_id += 1
        self.jobs[job_id] = {
            "id": job_id,
            "user_id": user_id,
            "business_id": business_id,
            "status": "pending",
            "fetched_count": 0,
            "inserted_count": 0,
            "updated_count": 0,
            "error_message": None,
            "worker_id": None,
            "lease_expires_at": None,
            "heartbeat_at": None,
            "started_at": None,
            "completed_at": None,
            "active_business_id": business_id,
        }
        return job_id, True

    def get_oldest_pending_job(self):
        pending = [job for job in self.jobs.values() if job["status"] == "pending"]
        return copy.deepcopy(min(pending, key=lambda job: job["id"])) if pending else None

    def claim_job(self, job_id, worker_id, lease_seconds):
        job = self.jobs.get(job_id)
        if not job or job["status"] != "pending":
            return False
        now = datetime.utcnow()
        job.update({
            "status": "processing",
            "worker_id": worker_id,
            "started_at": job["started_at"] or now,
            "heartbeat_at": now,
            "lease_expires_at": now + timedelta(seconds=lease_seconds),
            "active_business_id": job["business_id"],
        })
        self.claim_snapshots.append(copy.deepcopy(job))
        return True

    def heartbeat_job(self, job_id, worker_id, lease_seconds):
        job = self.jobs.get(job_id)
        if not job or job["status"] != "processing" or job["worker_id"] != worker_id:
            return False
        now = datetime.utcnow()
        job["heartbeat_at"] = now
        job["lease_expires_at"] = now + timedelta(seconds=lease_seconds)
        return True

    def confirm_and_renew_ownership(self, job_id, worker_id, lease_seconds):
        job = self.jobs.get(job_id)
        now = datetime.utcnow()
        if (
            not job
            or job["status"] != "processing"
            or job["worker_id"] != worker_id
            or job["lease_expires_at"] is None
            or job["lease_expires_at"] <= now
        ):
            return False
        job["heartbeat_at"] = now
        job["lease_expires_at"] = now + timedelta(seconds=lease_seconds)
        return True

    def complete_job(self, job_id, worker_id, result):
        job = self.jobs.get(job_id)
        if not job or job["status"] != "processing" or job["worker_id"] != worker_id:
            return False
        job.update({
            "status": "completed",
            "fetched_count": result["fetched_count"],
            "inserted_count": result["inserted_count"],
            "updated_count": result["updated_count"],
            "completed_at": datetime.utcnow(),
            "worker_id": None,
            "lease_expires_at": None,
            "heartbeat_at": None,
            "active_business_id": None,
        })
        return True

    def fail_job(self, job_id, worker_id, error_message):
        job = self.jobs.get(job_id)
        if not job or job["status"] != "processing" or job["worker_id"] != worker_id:
            return False
        job.update({
            "status": "failed",
            "error_message": error_message,
            "completed_at": datetime.utcnow(),
            "worker_id": None,
            "lease_expires_at": None,
            "heartbeat_at": None,
            "active_business_id": None,
        })
        return True

    def recover_expired_processing_jobs(self, legacy_timeout_minutes):
        now = datetime.utcnow()
        recovered = 0
        for job in self.jobs.values():
            expired = job["lease_expires_at"] is not None and job["lease_expires_at"] < now
            legacy = (
                job["lease_expires_at"] is None
                and job["status"] == "processing"
                and job["started_at"] < now - timedelta(minutes=legacy_timeout_minutes)
            )
            if job["status"] == "processing" and (expired or legacy):
                job.update({
                    "status": "pending",
                    "worker_id": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "started_at": None,
                    "active_business_id": job["business_id"],
                })
                recovered += 1
        return recovered

    def get_job(self, job_id, user_id=None):
        job = self.jobs.get(job_id)
        if not job or (user_id is not None and job["user_id"] != user_id):
            return None
        return copy.deepcopy(job)

    def get_active_job(self, business_id, user_id):
        for job in reversed(list(self.jobs.values())):
            if (
                job["business_id"] == business_id
                and job["user_id"] == user_id
                and job["status"] in {"pending", "processing"}
            ):
                return copy.deepcopy(job)
        return None


class InMemoryReviewCursor:
    def __init__(self, reviews):
        self.reviews = reviews
        self._selected = None
        self.lastrowid = 0

    def execute(self, query, params=()):
        normalized = " ".join(query.split())
        if normalized.startswith("SELECT id, review_text"):
            business_id, google_id, _external_id, location_id = params
            self._selected = next((
                review for review in self.reviews
                if review["business_id"] == int(business_id)
                and review["google_review_id"] == google_id
                and review["google_location_id"] == location_id
            ), None)
            return
        if normalized.startswith("INSERT INTO reviews"):
            self.lastrowid = max([review["id"] for review in self.reviews] or [0]) + 1
            self.reviews.append({
                "id": self.lastrowid,
                "business_id": int(params[0]),
                "rating": params[2],
                "review_text": params[5],
                "google_review_id": params[11],
                "google_location_id": params[12],
                "review_updated_at": params[9],
            })
            return
        if normalized.startswith("UPDATE reviews"):
            review_id = params[-1]
            review = next(item for item in self.reviews if item["id"] == review_id)
            review["rating"] = params[0] if "rating=%s" in normalized else review["rating"]
            if "review_text=%s" in normalized:
                review["review_text"] = params[2]
            return
        raise AssertionError(f"Unexpected review SQL: {normalized}")

    def fetchone(self):
        return copy.deepcopy(self._selected)


def google_response(status, payload):
    response = MagicMock()
    response.status_code = status
    response.ok = 200 <= status < 300
    response.headers = {}
    response.url = "https://mybusiness.googleapis.com/v4/accounts/1/locations/2/reviews"
    response.json.return_value = payload
    response.text = ""
    return response


class MockedGoogleReviewSyncEndToEndTests(unittest.TestCase):
    def setUp(self):
        worker.shutdown_requested = False
        worker.shutdown_event.clear()
        self.store = InMemoryGoogleJobStore()
        self.reviews = [{
            "id": 1,
            "business_id": 9,
            "rating": 2,
            "review_text": "Old text",
            "google_review_id": "review-1",
            "google_location_id": "locations/2",
            "review_updated_at": None,
        }]
        self.connection = {
            "access_token": "mock-access-token",
            "google_account_id": "accounts/1",
            "google_location_id": "locations/2",
            "business_id": 9,
        }
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY="mocked-e2e-secret")
        init_csrf(self.app)
        self.app.register_blueprint(google_business_bp)
        self.client = self.app.test_client()
        with self.client.session_transaction() as active_session:
            active_session["user_id"] = 7
            active_session["role"] = "owner"
            active_session[CSRF_SESSION_KEY] = "mocked-csrf-token"

    def enqueue(self, token="mocked-csrf-token"):
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["X-CSRF-Token"] = token
        return self.client.post(
            "/businesses/9/google/review-sync-jobs", json={}, headers=headers
        )

    def route_patches(self):
        return (
            patch("app.services.subscription_service.has_active_subscription", return_value=True),
            patch("app.routes.google_business.user_owns_business", return_value=True),
            patch("app.routes.google_business._get_connection_row", return_value=self.connection),
            patch("app.routes.google_business.google_review_sync_jobs", self.store),
        )

    def test_complete_mocked_workflow_updates_existing_review_without_duplicates(self):
        pages = [
            google_response(200, {
                "reviews": [{
                    "reviewId": "review-1",
                    "starRating": "FIVE",
                    "comment": "Updated text",
                    "createTime": "2026-01-01T00:00:00Z",
                    "updateTime": "2026-01-02T00:00:00Z",
                }],
                "nextPageToken": "page-2",
            }),
            google_response(200, {"reviews": [{
                "reviewId": "review-2",
                "starRating": "FOUR",
                "comment": "New review",
                "createTime": "2026-01-03T00:00:00Z",
                "updateTime": "2026-01-03T00:00:00Z",
            }]}),
        ]
        route_patches = self.route_patches()
        with route_patches[0], route_patches[1], route_patches[2], route_patches[3]:
            enqueue_response = self.enqueue()
            self.assertEqual(202, enqueue_response.status_code)
            job_id = enqueue_response.get_json()["job_id"]

            def execute_mocked_sync(_user_id, _business_id):
                result = sync_google_reviews(
                    InMemoryReviewCursor(self.reviews),
                    self.connection,
                    allow_internal_api_retry=False,
                )
                result["google_location_id"] = "locations/2"
                return result

            with patch("worker.google_review_sync_jobs", self.store), \
                 patch("worker.claim_next_job", return_value=None), \
                 patch("worker.run_google_review_sync", side_effect=execute_mocked_sync), \
                 patch("app.services.google_business_service.requests.get", side_effect=pages), \
                 patch("app.services.google_review_post_sync_service.refresh_business_review_analytics") as analytics, \
                 patch("app.services.google_review_post_sync_service.create_analysis_job", return_value=(88, True)) as analysis:
                analytics.return_value = {"topic_result": {"inserted_topics": 2}}
                worker.run_worker_iteration()

            claimed = self.store.claim_snapshots[0]
            self.assertEqual("processing", claimed["status"])
            self.assertEqual(worker.WORKER_ID, claimed["worker_id"])
            self.assertIsNotNone(claimed["lease_expires_at"])
            completed = self.store.jobs[job_id]
            self.assertEqual("completed", completed["status"])
            self.assertIsNone(completed["worker_id"])
            self.assertIsNone(completed["lease_expires_at"])
            self.assertEqual(2, len(self.reviews))
            self.assertEqual("Updated text", self.reviews[0]["review_text"])
            analytics.assert_called_once()
            analysis.assert_called_once_with(7, 9, force_reanalysis=False)

            status = self.client.get(f"/google-review-sync-jobs/{job_id}/status")
            self.assertEqual("completed", status.get_json()["status"])
            self.assertEqual(2, status.get_json()["fetched_count"])

            repeated_pages = [copy.deepcopy(page) for page in pages]
            with patch(
                "app.services.google_business_service.requests.get",
                side_effect=repeated_pages,
            ):
                repeated = sync_google_reviews(
                    InMemoryReviewCursor(self.reviews),
                    self.connection,
                    allow_internal_api_retry=False,
                )
            self.assertEqual(0, repeated["inserted_count"])
            self.assertEqual(2, len(self.reviews))

    def test_second_page_transient_failure_retries_whole_sync_without_partial_writes(self):
        first_page = google_response(200, {
            "reviews": [{
                "reviewId": "review-1",
                "starRating": "FIVE",
                "comment": "Updated text",
                "createTime": "2026-01-01T00:00:00Z",
                "updateTime": "2026-01-02T00:00:00Z",
            }],
            "nextPageToken": "page-2",
        })
        second_page = google_response(200, {"reviews": [{
            "reviewId": "review-2",
            "starRating": "FOUR",
            "comment": "New review",
            "createTime": "2026-01-03T00:00:00Z",
            "updateTime": "2026-01-03T00:00:00Z",
        }]})
        responses = [
            first_page,
            google_response(503, {}),
            copy.deepcopy(first_page),
            second_page,
        ]
        delays = []

        def execute_sync(_user_id, _business_id):
            result = sync_google_reviews(
                InMemoryReviewCursor(self.reviews),
                self.connection,
                allow_internal_api_retry=False,
            )
            result["google_location_id"] = "locations/2"
            return result

        with patch("worker.run_google_review_sync", side_effect=execute_sync), \
             patch("app.services.google_business_service.requests.get", side_effect=responses), \
             patch.object(worker.Config, "GOOGLE_REVIEW_SYNC_MAX_RETRIES", 1):
            result = worker._run_google_review_sync_with_retries(
                {"id": 41, "user_id": 7, "business_id": 9},
                sleep=delays.append,
                jitter=lambda _low, _high: 0,
            )

        self.assertEqual(2, result["fetched_count"])
        self.assertEqual([worker.Config.GOOGLE_REVIEW_SYNC_BACKOFF_BASE_SECONDS], delays)
        self.assertEqual(2, len(self.reviews))
        self.assertEqual(1, sum(
            review["google_review_id"] == "review-1" for review in self.reviews
        ))
        self.assertEqual(1, sum(
            review["google_review_id"] == "review-2" for review in self.reviews
        ))

    def test_duplicate_enqueue_csrf_and_polling_contract(self):
        route_patches = self.route_patches()
        with route_patches[0], route_patches[1], route_patches[2], route_patches[3]:
            self.assertEqual(403, self.enqueue(token=None).status_code)
            self.assertEqual(403, self.enqueue(token="invalid").status_code)
            first = self.enqueue().get_json()
            second = self.enqueue().get_json()
            self.assertEqual(first["job_id"], second["job_id"])
            self.assertEqual(1, len(self.store.jobs))

            pending = self.client.get(
                f"/google-review-sync-jobs/{first['job_id']}/status"
            ).get_json()
            self.assertEqual("pending", pending["status"])
            active = self.client.get(
                "/businesses/9/google/review-sync-jobs/active"
            ).get_json()
            self.assertEqual(first["job_id"], active["job_id"])

            self.store.claim_job(first["job_id"], "worker-a", 120)
            processing = self.client.get(
                f"/google-review-sync-jobs/{first['job_id']}/status"
            ).get_json()
            self.assertEqual("processing", processing["status"])
            self.store.fail_job(first["job_id"], "worker-a", "safe failure")
            failed = self.client.get(
                f"/google-review-sync-jobs/{first['job_id']}/status"
            ).get_json()
            self.assertEqual("failed", failed["status"])
            self.assertEqual(
                404,
                self.client.get(
                    "/businesses/9/google/review-sync-jobs/active"
                ).status_code,
            )

    def test_two_workers_lease_recovery_and_restart(self):
        job_id, _created = self.store.create_job(7, 9)
        self.assertTrue(self.store.claim_job(job_id, "worker-a", 120))
        self.assertFalse(self.store.claim_job(job_id, "worker-b", 120))
        self.assertTrue(self.store.heartbeat_job(job_id, "worker-a", 120))
        self.assertFalse(self.store.heartbeat_job(job_id, "worker-b", 120))
        with patch("worker.google_review_sync_jobs", self.store):
            self.assertTrue(worker._recover_stale_google_jobs())
        self.assertEqual("processing", self.store.jobs[job_id]["status"])

        self.store.jobs[job_id]["lease_expires_at"] = datetime.utcnow() - timedelta(seconds=1)
        with patch("worker.google_review_sync_jobs", self.store):
            self.assertTrue(worker._recover_stale_google_jobs())
        self.assertEqual("pending", self.store.jobs[job_id]["status"])
        self.assertFalse(self.store.complete_job(job_id, "worker-a", {
            "fetched_count": 1, "inserted_count": 1, "updated_count": 0
        }))
        self.assertTrue(self.store.claim_job(job_id, "worker-b", 120))
        self.assertTrue(self.store.complete_job(job_id, "worker-b", {
            "fetched_count": 1, "inserted_count": 1, "updated_count": 0
        }))

        legacy_id, _created = self.store.create_job(7, 10)
        legacy = self.store.jobs[legacy_id]
        legacy.update({
            "status": "processing",
            "worker_id": "legacy-worker",
            "lease_expires_at": None,
            "started_at": datetime.utcnow() - timedelta(minutes=31),
        })
        self.assertEqual(1, self.store.recover_expired_processing_jobs(30))
        self.assertEqual("pending", legacy["status"])

    @patch("worker.logger")
    def test_failure_sanitization_and_persistence_failures_are_contained(self, logger):
        job_id, _created = self.store.create_job(7, 9)
        self.store.claim_job(job_id, "worker-a", 120)
        with patch("worker.google_review_sync_jobs", self.store), \
             patch(
                 "worker.run_google_review_sync",
                 side_effect=RuntimeError(
                     "Authorization: Bearer access-secret refresh_token=refresh-secret "
                     "client_secret=client-secret"
                 ),
             ):
            worker._process_google_review_sync_job(self.store.get_job(job_id), "worker-a")

        stored = self.store.jobs[job_id]["error_message"]
        self.assertIn("[REDACTED]", stored)
        self.assertNotIn("access-secret", stored)
        self.assertNotIn("refresh-secret", stored)
        self.assertNotIn("client-secret", stored)
        logger.error.assert_called()
        self.assertNotIn("access-secret", repr(logger.error.call_args_list))
        self.assertNotIn("refresh-secret", repr(logger.error.call_args_list))
        self.assertNotIn("client-secret", repr(logger.error.call_args_list))

        second_id, _created = self.store.create_job(7, 9)
        self.store.claim_job(second_id, "worker-a", 120)
        original_complete = self.store.complete_job
        self.store.complete_job = MagicMock(side_effect=RuntimeError("database unavailable"))
        with patch("worker.google_review_sync_jobs", self.store), \
             patch("worker.run_google_review_sync", return_value={
                 "fetched_count": 0,
                 "inserted_count": 0,
                 "updated_count": 0,
                 "google_location_id": "locations/2",
             }), \
             patch("worker.perform_google_review_post_sync"):
            self.assertFalse(
                worker._process_google_review_sync_job(
                    self.store.get_job(second_id), "worker-a"
                )
            )
        self.assertEqual("processing", self.store.jobs[second_id]["status"])
        self.store.complete_job = original_complete


if __name__ == "__main__":
    unittest.main()
