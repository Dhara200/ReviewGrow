import logging
from datetime import datetime, timedelta
from uuid import uuid4

from app.services.analysis_job_service import (
    claim_analysis_job_by_id,
    create_analysis_job,
    process_analysis_job,
)
from app.services.business_analytics_service import refresh_business_review_analytics
from app.services.database_service import get_connection
from app.services.google_business_service import refresh_access_token
from app.services.review_sync_service import sync_google_reviews
from app.services.token_crypto_service import decrypt_token, encrypt_token


logger = logging.getLogger(__name__)
STALE_JOB_MINUTES = 45
DEFAULT_MAX_ATTEMPTS = 3


def create_google_review_sync_job(user_id, business_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        conn.start_transaction()
        cursor.execute("SELECT id FROM businesses WHERE id=%s FOR UPDATE", (business_id,))
        if not cursor.fetchone():
            raise ValueError("Business not found.")

        cursor.execute(
            """
            SELECT id FROM google_review_sync_jobs
            WHERE business_id=%s AND status IN ('pending','processing')
            ORDER BY created_at DESC LIMIT 1
            """,
            (business_id,),
        )
        active = cursor.fetchone()
        if active:
            conn.commit()
            return active["id"], False

        cursor.execute(
            """
            INSERT INTO google_review_sync_jobs
                (user_id, business_id, status, max_attempts, next_attempt_at)
            VALUES (%s,%s,'pending',%s,NOW())
            """,
            (user_id, business_id, DEFAULT_MAX_ATTEMPTS),
        )
        job_id = cursor.lastrowid
        conn.commit()
        return job_id, True
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def get_google_review_sync_job(job_id, user_id, is_admin=False):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        sql = "SELECT * FROM google_review_sync_jobs WHERE id=%s"
        params = [job_id]
        if not is_admin:
            sql += " AND user_id=%s"
            params.append(user_id)
        cursor.execute(sql, tuple(params))
        job = cursor.fetchone()
        if not job:
            return None
        result = {
            "job_id": job["id"],
            "business_id": job["business_id"],
            "status": job["status"],
            "attempts": job["attempts"],
            "max_attempts": job["max_attempts"],
            "error_message": job["error_message"],
            "fetched_count": job["fetched_count"],
            "inserted_count": job["inserted_count"],
            "updated_count": job["updated_count"],
            "topics_inserted": job["topics_inserted"],
            "analysis_job_id": job["analysis_job_id"],
        }
        for field in ("created_at", "started_at", "completed_at", "next_attempt_at"):
            result[field] = job[field].isoformat() if job.get(field) else None
        return result
    finally:
        cursor.close()
        conn.close()


def reset_stale_google_review_sync_jobs(timeout_minutes=STALE_JOB_MINUTES):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            UPDATE google_review_sync_jobs
            SET status=CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'pending' END,
                completed_at=CASE WHEN attempts >= max_attempts THEN NOW() ELSE NULL END,
                next_attempt_at=CASE WHEN attempts >= max_attempts THEN next_attempt_at ELSE NOW() END,
                claimed_by=NULL,
                started_at=NULL,
                heartbeat_at=NULL,
                error_message='Worker lease expired before the sync completed.'
            WHERE status='processing'
              AND COALESCE(heartbeat_at, started_at) < DATE_SUB(NOW(), INTERVAL %s MINUTE)
            """,
            (timeout_minutes,),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        cursor.close()
        conn.close()


def claim_next_google_review_sync_job(worker_id=None):
    worker_id = worker_id or uuid4().hex
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        conn.start_transaction()
        cursor.execute(
            """
            SELECT * FROM google_review_sync_jobs
            WHERE status='pending' AND next_attempt_at <= NOW()
            ORDER BY created_at ASC LIMIT 1
            FOR UPDATE SKIP LOCKED
            """
        )
        job = cursor.fetchone()
        if not job:
            conn.commit()
            return None
        cursor.execute(
            """
            UPDATE google_review_sync_jobs
            SET status='processing', attempts=attempts+1, claimed_by=%s,
                started_at=NOW(), heartbeat_at=NOW(), completed_at=NULL,
                error_message=NULL
            WHERE id=%s AND status='pending'
            """,
            (worker_id, job["id"]),
        )
        conn.commit()
        job["claimed_by"] = worker_id
        job["attempts"] += 1
        return job
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def process_google_review_sync_job(job):
    lock_conn = get_connection()
    lock_cursor = lock_conn.cursor(dictionary=True)
    lock_name = f"reviewgrow:google-review-sync:{job['business_id']}"
    try:
        lock_cursor.execute("SELECT GET_LOCK(%s, 0) AS acquired", (lock_name,))
        if not (lock_cursor.fetchone() or {}).get("acquired"):
            _retry_or_fail(job, "Another worker is already syncing this business.", consume_attempt=False)
            return

        connection = _load_worker_connection(job["business_id"])
        connection = _ensure_valid_token(connection)
        _heartbeat(job)

        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            result = sync_google_reviews(cursor, connection)
            cursor.execute(
                "UPDATE google_business_connections SET last_sync_at=NOW() WHERE id=%s",
                (connection["id"],),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()

        topics_inserted = 0
        if result["inserted_count"] or result["updated_count"]:
            analytics = refresh_business_review_analytics(
                job["business_id"],
                mark_consultant_outdated=True,
                source="google",
                google_location_id=connection.get("google_location_id"),
                require_google_review_id=True,
            )
            topics_inserted = analytics["topic_result"].get("inserted_topics", 0)

        _heartbeat(job)
        analysis_job_id, _created = create_analysis_job(
            connection["user_id"], job["business_id"], force_reanalysis=False
        )
        if claim_analysis_job_by_id(analysis_job_id):
            process_analysis_job(analysis_job_id)

        _complete_job(job, result, topics_inserted, analysis_job_id)
    except Exception as error:
        logger.exception(
            "Google review sync job failed: job_id=%s business_id=%s attempt=%s",
            job["id"], job["business_id"], job["attempts"],
        )
        _retry_or_fail(job, str(error))
    finally:
        try:
            lock_cursor.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
        except Exception:
            logger.exception("Failed to release Google sync advisory lock %s", lock_name)
        lock_cursor.close()
        lock_conn.close()


def _load_worker_connection(business_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT * FROM google_business_connections
            WHERE business_id=%s AND is_connected=TRUE LIMIT 1
            """,
            (business_id,),
        )
        connection = cursor.fetchone()
        if not connection:
            raise ValueError("Google Business Profile is not connected.")
        if not connection.get("google_account_id") or not connection.get("google_location_id"):
            raise ValueError("Google Business Profile location is not selected.")
        connection["access_token"] = decrypt_token(connection.get("access_token"))
        connection["refresh_token"] = decrypt_token(connection.get("refresh_token"))
        return connection
    finally:
        cursor.close()
        conn.close()


def _ensure_valid_token(connection):
    expiry = connection.get("token_expiry")
    if expiry and expiry > datetime.utcnow() + timedelta(minutes=5):
        return connection
    token_data = refresh_access_token(connection.get("refresh_token"))
    conn = get_connection()
    cursor = conn.cursor()
    try:
        scopes = token_data.get("scope")
        cursor.execute(
            """
            UPDATE google_business_connections
            SET access_token=%s, token_expiry=%s,
                scope=COALESCE(%s, scope), scopes=COALESCE(%s, scopes)
            WHERE id=%s AND business_id=%s
            """,
            (
                encrypt_token(token_data["access_token"]), token_data["token_expiry"],
                scopes, scopes, connection["id"], connection["business_id"],
            ),
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()
    connection["access_token"] = token_data["access_token"]
    connection["token_expiry"] = token_data["token_expiry"]
    return connection


def _heartbeat(job):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            UPDATE google_review_sync_jobs SET heartbeat_at=NOW()
            WHERE id=%s AND status='processing' AND claimed_by=%s
            """,
            (job["id"], job["claimed_by"]),
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def _complete_job(job, result, topics_inserted, analysis_job_id):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            UPDATE google_review_sync_jobs
            SET status='completed', completed_at=NOW(), heartbeat_at=NOW(),
                fetched_count=%s, inserted_count=%s, updated_count=%s,
                topics_inserted=%s, analysis_job_id=%s, error_message=NULL
            WHERE id=%s AND status='processing' AND claimed_by=%s
            """,
            (
                result["fetched_count"], result["inserted_count"], result["updated_count"],
                topics_inserted, analysis_job_id, job["id"], job["claimed_by"],
            ),
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def _retry_or_fail(job, message, consume_attempt=True):
    attempts = job["attempts"] if consume_attempt else max(0, job["attempts"] - 1)
    terminal = attempts >= job["max_attempts"]
    delay_seconds = min(300, 15 * (2 ** max(0, attempts - 1)))
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            UPDATE google_review_sync_jobs
            SET status=%s, attempts=%s, claimed_by=NULL, started_at=NULL, heartbeat_at=NULL,
                completed_at=CASE WHEN %s THEN NOW() ELSE NULL END,
                next_attempt_at=DATE_ADD(NOW(), INTERVAL %s SECOND), error_message=%s
            WHERE id=%s AND status='processing' AND claimed_by=%s
            """,
            (
                "failed" if terminal else "pending", attempts, terminal, delay_seconds,
                message[:2000], job["id"], job["claimed_by"],
            ),
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()
