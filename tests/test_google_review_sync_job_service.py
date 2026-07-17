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

        claimed = service.claim_job(41)

        self.assertTrue(claimed)
        self.assertEqual(1, connection.commits)
        self.assertIn("AND status='pending'", cursor.executions[0][0])
        self.assertIn("active_business_id=business_id", cursor.executions[0][0])

    def test_oldest_pending_job_is_discovered(self):
        expected = {"id": 11, "status": "pending"}
        cursor = FakeCursor(fetch_results=[expected])
        service, _connection = self.service_for(cursor)

        result = service.get_oldest_pending_job()

        self.assertEqual(expected, result)
        self.assertIn("WHERE status='pending'", cursor.executions[0][0])
        self.assertIn("ORDER BY created_at ASC, id ASC", cursor.executions[0][0])

    def test_stale_processing_jobs_are_recovered(self):
        cursor = FakeCursor(rowcounts=[2])
        service, connection = self.service_for(cursor)

        recovered = service.recover_stale_processing_jobs(30)

        self.assertEqual(2, recovered)
        self.assertEqual(1, connection.commits)
        query, params = cursor.executions[0]
        self.assertIn("SET status='pending'", query)
        self.assertIn("started_at=NULL", query)
        self.assertIn("completed_at=NULL", query)
        self.assertIn("error_message=NULL", query)
        self.assertIn("active_business_id=business_id", query)
        self.assertIn("WHERE status='processing'", query)
        self.assertEqual((30,), params)

    def test_stale_recovery_rejects_non_positive_timeout(self):
        service, connection = self.service_for(FakeCursor())

        with self.assertRaises(ValueError):
            service.recover_stale_processing_jobs(0)

        self.assertFalse(connection.closed)

    def test_only_one_of_two_workers_can_claim_the_same_job(self):
        first_service, _first_connection = self.service_for(FakeCursor(rowcounts=[1]))
        second_service, _second_connection = self.service_for(FakeCursor(rowcounts=[0]))

        first_claim = first_service.claim_job(41)
        second_claim = second_service.claim_job(41)

        self.assertTrue(first_claim)
        self.assertFalse(second_claim)

    def test_update_job_keeps_active_marker_for_pending_and_processing(self):
        for status in ("pending", "processing"):
            with self.subTest(status=status):
                cursor = FakeCursor(rowcounts=[1])
                service, connection = self.service_for(cursor)

                updated = service.update_job(41, status=status)

                self.assertTrue(updated)
                self.assertEqual(1, connection.commits)
                self.assertIn("active_business_id=business_id", cursor.executions[0][0])
                self.assertEqual((status, 41), cursor.executions[0][1])

    def test_update_job_clears_active_marker_for_completed_and_failed(self):
        for status in ("completed", "failed"):
            with self.subTest(status=status):
                cursor = FakeCursor(rowcounts=[1])
                service, connection = self.service_for(cursor)

                updated = service.update_job(41, status=status)

                self.assertTrue(updated)
                self.assertEqual(1, connection.commits)
                self.assertIn("active_business_id=NULL", cursor.executions[0][0])
                self.assertEqual((status, 41), cursor.executions[0][1])

    def test_update_job_rejects_unknown_fields(self):
        service, connection = self.service_for(FakeCursor())

        with self.assertRaises(ValueError):
            service.update_job(41, claimed_by="worker-1")

        self.assertFalse(connection.closed)

    def test_get_job_can_be_scoped_to_user(self):
        expected = {"id": 41, "user_id": 7, "status": "pending"}
        cursor = FakeCursor(fetch_results=[expected])
        service, _connection = self.service_for(cursor)

        result = service.get_job(41, user_id=7)

        self.assertEqual(expected, result)
        self.assertIn("AND user_id=%s", cursor.executions[0][0])
        self.assertEqual((41, 7), cursor.executions[0][1])


if __name__ == "__main__":
    unittest.main()
