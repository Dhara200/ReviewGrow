import mysql.connector

from app.services.database_service import get_connection


JOB_STATUSES = frozenset({"pending", "processing", "completed", "failed"})
UPDATABLE_FIELDS = frozenset({
    "status",
    "fetched_count",
    "inserted_count",
    "updated_count",
    "error_message",
    "started_at",
    "completed_at",
})


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

    def claim_job(self, job_id):
        connection = self._connection_factory()
        cursor = connection.cursor(dictionary=True)

        try:
            cursor.execute(
                """
                UPDATE google_review_sync_jobs
                SET status='processing',
                    active_business_id=business_id,
                    started_at=COALESCE(started_at, NOW()),
                    completed_at=NULL,
                    error_message=NULL
                WHERE id=%s
                  AND status='pending'
                """,
                (job_id,),
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

    def update_job(self, job_id, **fields):
        if not fields:
            return False

        invalid_fields = set(fields) - UPDATABLE_FIELDS
        if invalid_fields:
            names = ", ".join(sorted(invalid_fields))
            raise ValueError(f"Unsupported Google review sync job fields: {names}")

        status = fields.get("status")
        if status is not None and status not in JOB_STATUSES:
            raise ValueError(f"Unsupported Google review sync job status: {status}")

        assignments = [f"{field}=%s" for field in fields]
        if status in {"pending", "processing"}:
            assignments.append("active_business_id=business_id")
        elif status in {"completed", "failed"}:
            assignments.append("active_business_id=NULL")

        assignments_sql = ", ".join(assignments)
        params = [fields[field] for field in fields]
        params.append(job_id)
        connection = self._connection_factory()
        cursor = connection.cursor()

        try:
            cursor.execute(
                f"UPDATE google_review_sync_jobs SET {assignments_sql} WHERE id=%s",
                tuple(params),
            )
            updated = cursor.rowcount == 1
            connection.commit()
            return updated
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
