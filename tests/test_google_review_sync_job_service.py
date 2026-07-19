import unittest

import mysql.connector

from app.services.google_review_sync_job_service import GoogleReviewSyncJobService


class FakeCursor:
    def __init__(self, fetch_results=None, rowcounts=None, lastrowid=0, execute_errors=None):
        self.fetch_results = list(fetch_results or [])
        self.rowcounts = list(rowcounts or [])
        self.lastrowid = lastrowid
        self.rowcount = 0
        self.executions = []
        self.execute_errors = list(execute_errors or [])
        self.closed = False

    def execute(self, query, params=()):
        self.executions.append((" ".join(query.split()), params))
        error = self.execute_errors.pop(0) if self.execute_errors else None
        if error:
            raise error
        self.rowcount = self.rowcounts.pop(0) if self.rowcounts else 0

    def fetchone(self):
        return self.fetch_results.pop(0) if self.fetch_results else None

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self, dictionary=False):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class GoogleReviewSyncJobServiceTests(unittest.TestCase):
    def service_for(self, cursor):
        connection = FakeConnection(cursor)
        return GoogleReviewSyncJobService(lambda: connection), connection

    def test_create_job_inserts_pending_job(self):
        cursor = FakeCursor(fetch_results=[None], lastrowid=41)
        service, connection = self.service_for(cursor)

        result = service.create_job(7, 9)

        self.assertEqual((41, True), result)
        self.assertEqual(1, connection.commits)
        self.assertIn("INSERT INTO google_review_sync_jobs", cursor.executions[1][0])
        self.assertIn("active_business_id", cursor.executions[1][0])
        self.assertEqual((7, 9, 9), cursor.executions[1][1])

    def test_create_job_returns_existing_active_job(self):
        cursor = FakeCursor(fetch_results=[{"id": 23}])
        service, connection = self.service_for(cursor)

        result = service.create_job(7, 9)

        self.assertEqual((23, False), result)
        self.assertEqual(0, connection.commits)
        self.assertEqual(1, len(cursor.executions))

    def test_create_job_returns_winning_job_after_duplicate_key_collision(self):
        duplicate_error = mysql.connector.IntegrityError(
            msg="Duplicate entry",
            errno=1062,
        )
        cursor = FakeCursor(
            fetch_results=[None, {"id": 52}],
            execute_errors=[None, duplicate_error, None],
        )
        service, connection = self.service_for(cursor)

        result = service.create_job(7, 9)

        self.assertEqual((52, False), result)
        self.assertEqual(1, connection.rollbacks)
        self.assertEqual(0, connection.commits)
        self.assertEqual(3, len(cursor.executions))
        self.assertIn("status IN ('pending', 'processing')", cursor.executions[2][0])
        self.assertEqual((9,), cursor.executions[2][1])

    def test_create_job_reraises_non_duplicate_integrity_error(self):
        integrity_error = mysql.connector.IntegrityError(
            msg="Foreign key constraint fails",
            errno=1452,
        )
        cursor = FakeCursor(
            fetch_results=[None],
            execute_errors=[None, integrity_error],
        )
        service, connection = self.service_for(cursor)

        with self.assertRaises(mysql.connector.IntegrityError) as raised:
            service.create_job(7, 9)

        self.assertEqual(1452, raised.exception.errno)
        self.assertEqual(1, connection.rollbacks)

    def test_create_job_reraises_duplicate_error_when_active_job_is_missing(self):
        duplicate_error = mysql.connector.IntegrityError(
            msg="Duplicate entry",
            errno=1062,
        )
        cursor = FakeCursor(
            fetch_results=[None, None],
            execute_errors=[None, duplicate_error, None],
        )
        service, connection = self.service_for(cursor)

        with self.assertRaises(mysql.connector.IntegrityError) as raised:
            service.create_job(7, 9)

        self.assertEqual(1062, raised.exception.errno)
        self.assertEqual(1, connection.rollbacks)

    def test_claim_job_only_claims_pending_row(self):
        cursor = FakeCursor(rowcounts=[1])
        service, connection = self.service_for(cursor)

        claimed = service.claim_job(41, "worker-a", 120)

        self.assertTrue(claimed)
        self.assertEqual(1, connection.commits)
        self.assertIn("AND status='pending'", cursor.executions[0][0])
        self.assertIn("active_business_id=business_id", cursor.executions[0][0])
        self.assertIn("worker_id=%s", cursor.executions[0][0])
        self.assertIn("heartbeat_at=UTC_TIMESTAMP(6)", cursor.executions[0][0])
        self.assertIn("lease_expires_at=DATE_ADD", cursor.executions[0][0])
        self.assertEqual(("worker-a", 120, 41), cursor.executions[0][1])

    def test_oldest_pending_job_is_discovered(self):
        expected = {"id": 11, "status": "pending"}
        cursor = FakeCursor(fetch_results=[expected])
        service, _connection = self.service_for(cursor)

        result = service.get_oldest_pending_job()

        self.assertEqual(expected, result)
        self.assertIn("WHERE status='pending'", cursor.executions[0][0])
        self.assertIn("ORDER BY created_at ASC, id ASC", cursor.executions[0][0])

    def test_expired_and_legacy_stale_processing_jobs_are_recovered(self):
        cursor = FakeCursor(rowcounts=[2])
        service, connection = self.service_for(cursor)

        recovered = service.recover_expired_processing_jobs(30)

        self.assertEqual(2, recovered)
        self.assertEqual(1, connection.commits)
        query, params = cursor.executions[0]
        self.assertIn("SET status='pending'", query)
        self.assertIn("started_at=NULL", query)
        self.assertIn("completed_at=NULL", query)
        self.assertIn("error_message=NULL", query)
        self.assertIn("worker_id=NULL", query)
        self.assertIn("lease_expires_at=NULL", query)
        self.assertIn("heartbeat_at=NULL", query)
        self.assertIn("active_business_id=business_id", query)
        self.assertIn("WHERE status='processing'", query)
        self.assertIn("lease_expires_at < UTC_TIMESTAMP(6)", query)
        self.assertIn("lease_expires_at IS NULL", query)
        self.assertEqual((30,), params)

    def test_stale_recovery_rejects_non_positive_timeout(self):
        service, connection = self.service_for(FakeCursor())

        with self.assertRaises(ValueError):
            service.recover_expired_processing_jobs(0)

        self.assertFalse(connection.closed)

    def test_only_one_of_two_workers_can_claim_the_same_job(self):
        first_service, _first_connection = self.service_for(FakeCursor(rowcounts=[1]))
        second_service, _second_connection = self.service_for(FakeCursor(rowcounts=[0]))

        first_claim = first_service.claim_job(41, "worker-a", 120)
        second_claim = second_service.claim_job(41, "worker-b", 120)

        self.assertTrue(first_claim)
        self.assertFalse(second_claim)

    def test_heartbeat_is_guarded_by_processing_owner(self):
        for rowcount, expected in ((1, True), (0, False)):
            with self.subTest(rowcount=rowcount):
                cursor = FakeCursor(rowcounts=[rowcount])
                service, connection = self.service_for(cursor)

                renewed = service.heartbeat_job(41, "worker-a", 120)

                self.assertEqual(expected, renewed)
                query, params = cursor.executions[0]
                self.assertIn("AND status='processing'", query)
                self.assertIn("AND worker_id=%s", query)
                self.assertEqual((120, 41, "worker-a"), params)
                self.assertEqual(1, connection.commits)

    def test_ownership_confirmation_requires_unexpired_owned_processing_lease(self):
        for rowcount, expected in ((1, True), (0, False)):
            with self.subTest(rowcount=rowcount):
                cursor = FakeCursor(rowcounts=[rowcount])
                service, connection = self.service_for(cursor)

                confirmed = service.confirm_and_renew_ownership(
                    41, "worker-a", 120
                )

                self.assertEqual(expected, confirmed)
                query, params = cursor.executions[0]
                self.assertIn("AND status='processing'", query)
                self.assertIn("AND worker_id=%s", query)
                self.assertIn("lease_expires_at IS NOT NULL", query)
                self.assertIn("lease_expires_at > UTC_TIMESTAMP(6)", query)
                self.assertIn("heartbeat_at=UTC_TIMESTAMP(6)", query)
                self.assertIn("lease_expires_at=DATE_ADD", query)
                self.assertIn("updated_at=UTC_TIMESTAMP(6)", query)
                self.assertEqual((120, 41, "worker-a"), params)
                self.assertEqual(1, connection.commits)
                self.assertTrue(cursor.closed)
                self.assertTrue(connection.closed)

    def test_ownership_confirmation_rolls_back_and_closes_on_database_error(self):
        cursor = FakeCursor(execute_errors=[RuntimeError("database unavailable")])
        service, connection = self.service_for(cursor)

        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            service.confirm_and_renew_ownership(41, "worker-a", 120)

        self.assertEqual(1, connection.rollbacks)
        self.assertTrue(cursor.closed)
        self.assertTrue(connection.closed)

    def test_completion_is_owner_guarded_and_clears_lease_metadata(self):
        cursor = FakeCursor(rowcounts=[1])
        service, _connection = self.service_for(cursor)

        completed = service.complete_job(
            41,
            "worker-a",
            {"fetched_count": 5, "inserted_count": 2, "updated_count": 1},
        )

        self.assertTrue(completed)
        query, params = cursor.executions[0]
        self.assertIn("AND worker_id=%s", query)
        self.assertIn("worker_id=NULL", query)
        self.assertIn("lease_expires_at=NULL", query)
        self.assertIn("heartbeat_at=NULL", query)
        self.assertIn("active_business_id=NULL", query)
        self.assertEqual(("completed", None, 5, 2, 1, 41, "worker-a"), params)

    def test_failure_is_owner_guarded_and_clears_lease_metadata(self):
        cursor = FakeCursor(rowcounts=[0])
        service, _connection = self.service_for(cursor)

        failed = service.fail_job(41, "other-worker", "safe failure")

        self.assertFalse(failed)
        query, params = cursor.executions[0]
        self.assertIn("AND worker_id=%s", query)
        self.assertIn("active_business_id=NULL", query)
        self.assertEqual(("failed", "safe failure", 41, "other-worker"), params)

    def test_get_job_can_be_scoped_to_user(self):
        expected = {"id": 41, "user_id": 7, "status": "pending"}
        cursor = FakeCursor(fetch_results=[expected])
        service, _connection = self.service_for(cursor)

        result = service.get_job(41, user_id=7)

        self.assertEqual(expected, result)
        self.assertIn("AND user_id=%s", cursor.executions[0][0])
        self.assertEqual((41, 7), cursor.executions[0][1])

    def test_get_active_job_is_scoped_to_business_and_user(self):
        expected = {"id": 41, "business_id": 9, "user_id": 7, "status": "processing"}
        cursor = FakeCursor(fetch_results=[expected])
        service, _connection = self.service_for(cursor)

        result = service.get_active_job(9, user_id=7)

        self.assertEqual(expected, result)
        query, params = cursor.executions[0]
        self.assertIn("business_id=%s", query)
        self.assertIn("user_id=%s", query)
        self.assertIn("status IN ('pending', 'processing')", query)
        self.assertEqual((9, 7), params)


if __name__ == "__main__":
    unittest.main()
