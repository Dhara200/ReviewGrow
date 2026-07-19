import mysql.connector

from app.services.database_service import get_connection


class GoogleReviewSyncJobService:
    """Data access for the future Google review synchronization queue."""

    def __init__(self, connection_factory=get_connection):
        self._connection_factory = connection_factory

    def create_job(self, user_id, business_id):
        connection = self._connection_factory()
        cursor = connection.cursor(dictionary=True)

        try:
            cursor.execute(
                """
                SELECT id
                FROM google_review_sync_jobs
                WHERE business_id=%s
                  AND status IN ('pending', 'processing')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (business_id,),
            )
            active_job = cursor.fetchone()
            if active_job:
                return active_job["id"], False

            cursor.execute(
                """
                INSERT INTO google_review_sync_jobs
                    (user_id, business_id, active_business_id, status)
                VALUES (%s, %s, %s, 'pending')
                """,
                (user_id, business_id, business_id),
            )
            job_id = cursor.lastrowid
            connection.commit()
            return job_id, True
        except mysql.connector.IntegrityError as error:
            connection.rollback()
            if error.errno != 1062:
                raise

            cursor.execute(
                """
                SELECT id
                FROM google_review_sync_jobs
                WHERE business_id=%s
                  AND status IN ('pending', 'processing')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (business_id,),
            )
            active_job = cursor.fetchone()
            if not active_job:
                raise

            return active_job["id"], False
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def claim_job(self, job_id, worker_id, lease_seconds):
        _validate_lease_arguments(worker_id, lease_seconds)
        connection = self._connection_factory()
        cursor = connection.cursor()

        try:
            cursor.execute(
                """
                UPDATE google_review_sync_jobs
                SET status='processing',
                    active_business_id=business_id,
                    worker_id=%s,
                    started_at=COALESCE(started_at, UTC_TIMESTAMP(6)),
                    heartbeat_at=UTC_TIMESTAMP(6),
                    lease_expires_at=DATE_ADD(
                        UTC_TIMESTAMP(6), INTERVAL %s SECOND
                    ),
                    completed_at=NULL,
                    error_message=NULL
                WHERE id=%s
                  AND status='pending'
                """,
                (worker_id, lease_seconds, job_id),
            )
            claimed = cursor.rowcount == 1
            connection.commit()
            return claimed
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def heartbeat_job(self, job_id, worker_id, lease_seconds):
        _validate_lease_arguments(worker_id, lease_seconds)
        connection = self._connection_factory()
        cursor = connection.cursor()

        try:
            cursor.execute(
                """
                UPDATE google_review_sync_jobs
                SET heartbeat_at=UTC_TIMESTAMP(6),
                    lease_expires_at=DATE_ADD(
                        UTC_TIMESTAMP(6), INTERVAL %s SECOND
                    )
                WHERE id=%s
                  AND status='processing'
                  AND worker_id=%s
                """,
                (lease_seconds, job_id, worker_id),
            )
            renewed = cursor.rowcount == 1
            connection.commit()
            return renewed
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def confirm_and_renew_ownership(self, job_id, worker_id, lease_seconds):
        """Confirm an unexpired processing lease and renew it atomically."""
        _validate_lease_arguments(worker_id, lease_seconds)
        connection = self._connection_factory()
        cursor = connection.cursor()

        try:
            cursor.execute(
                """
                UPDATE google_review_sync_jobs
                SET heartbeat_at=UTC_TIMESTAMP(6),
                    lease_expires_at=DATE_ADD(
                        UTC_TIMESTAMP(6), INTERVAL %s SECOND
                    ),
                    updated_at=UTC_TIMESTAMP(6)
                WHERE id=%s
                  AND status='processing'
                  AND worker_id=%s
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at > UTC_TIMESTAMP(6)
                """,
                (lease_seconds, job_id, worker_id),
            )
            confirmed = cursor.rowcount == 1
            connection.commit()
            return confirmed
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def complete_job(self, job_id, worker_id, result):
        return self._finalize_job(
            job_id,
            worker_id,
            status="completed",
            fetched_count=result["fetched_count"],
            inserted_count=result["inserted_count"],
            updated_count=result["updated_count"],
            error_message=None,
        )

    def fail_job(self, job_id, worker_id, error_message):
        return self._finalize_job(
            job_id,
            worker_id,
            status="failed",
            error_message=error_message,
        )

    def _finalize_job(self, job_id, worker_id, status, error_message, **counts):
        if not worker_id:
            raise ValueError("Worker ID is required.")
        assignments = [
            "status=%s",
            "error_message=%s",
            "completed_at=UTC_TIMESTAMP(6)",
            "worker_id=NULL",
            "lease_expires_at=NULL",
            "heartbeat_at=NULL",
            "active_business_id=NULL",
        ]
        params = [status, error_message]
        for field in ("fetched_count", "inserted_count", "updated_count"):
            if field in counts:
                assignments.append(f"{field}=%s")
                params.append(counts[field])
        params.extend((job_id, worker_id))
        connection = self._connection_factory()
        cursor = connection.cursor()

        try:
            cursor.execute(
                f"""
                UPDATE google_review_sync_jobs
                SET {', '.join(assignments)}
                WHERE id=%s
                  AND status='processing'
                  AND worker_id=%s
                """,
                tuple(params),
            )
            finalized = cursor.rowcount == 1
            connection.commit()
            return finalized
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def get_oldest_pending_job(self):
        connection = self._connection_factory()
        cursor = connection.cursor(dictionary=True)

        try:
            cursor.execute(
                """
                SELECT *
                FROM google_review_sync_jobs
                WHERE status='pending'
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """
            )
            return cursor.fetchone()
        finally:
            cursor.close()
            connection.close()

    def recover_expired_processing_jobs(self, legacy_timeout_minutes):
        if legacy_timeout_minutes <= 0:
            raise ValueError("Stale processing timeout must be greater than zero.")

        connection = self._connection_factory()
        cursor = connection.cursor()

        try:
            cursor.execute(
                """
                UPDATE google_review_sync_jobs
                SET status='pending',
                    started_at=NULL,
                    completed_at=NULL,
                    error_message=NULL,
                    worker_id=NULL,
                    lease_expires_at=NULL,
                    heartbeat_at=NULL,
                    active_business_id=business_id
                WHERE status='processing'
                  AND (
                      lease_expires_at < UTC_TIMESTAMP(6)
                      OR (
                          lease_expires_at IS NULL
                          AND started_at < DATE_SUB(
                              UTC_TIMESTAMP(6), INTERVAL %s MINUTE
                          )
                      )
                  )
                """,
                (legacy_timeout_minutes,),
            )
            recovered_count = cursor.rowcount
            connection.commit()
            return recovered_count
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def get_job(self, job_id, user_id=None):
        connection = self._connection_factory()
        cursor = connection.cursor(dictionary=True)

        try:
            query = "SELECT * FROM google_review_sync_jobs WHERE id=%s"
            params = [job_id]
            if user_id is not None:
                query += " AND user_id=%s"
                params.append(user_id)

            cursor.execute(query, tuple(params))
            return cursor.fetchone()
        finally:
            cursor.close()
            connection.close()

    def get_active_job(self, business_id, user_id):
        connection = self._connection_factory()
        cursor = connection.cursor(dictionary=True)

        try:
            cursor.execute(
                """
                SELECT *
                FROM google_review_sync_jobs
                WHERE business_id=%s
                  AND user_id=%s
                  AND status IN ('pending', 'processing')
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (business_id, user_id),
            )
            return cursor.fetchone()
        finally:
            cursor.close()
            connection.close()


def _validate_lease_arguments(worker_id, lease_seconds):
    if not worker_id:
        raise ValueError("Worker ID is required.")
    if lease_seconds <= 0:
        raise ValueError("Lease duration must be greater than zero.")
