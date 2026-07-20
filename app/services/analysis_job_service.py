import json
import logging
import re
import time
import mysql.connector

from app.services.ai_service import AIResult, AIService, AIServiceError, log_ai_usage
from app.services.database_service import get_connection


ACTIVE_JOB_STATUSES = ("pending", "processing")
DEFAULT_BATCH_SIZE = 25
DEFAULT_LEASE_SECONDS = 120
logger = logging.getLogger(__name__)


def create_analysis_job(user_id, business_id, force_reanalysis=False):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        operation_key = f"review_analysis:{business_id}"

        if force_reanalysis:
            cursor.execute(
                """
                UPDATE reviews
                SET analysis_status='pending',
                    analysis_error=NULL
                WHERE business_id=%s
                """,
                (business_id,)
            )

        review_filter = """
            business_id=%s
            AND (
                analysis_status IS NULL
                OR analysis_status='pending'
            )
        """

        cursor.execute(
            f"""
            SELECT COUNT(*) AS total_reviews
            FROM reviews
            WHERE {review_filter}
            """,
            (business_id,)
        )
        total_reviews = cursor.fetchone()["total_reviews"]

        cursor.execute(
            """
            INSERT INTO analysis_jobs
            (
                user_id,
                business_id,
                job_type,
                operation_key,
                active_operation_key,
                status,
                total_reviews,
                processed_reviews,
                failed_reviews,
                force_reanalysis
            )
            VALUES (%s,%s,'review_analysis',%s,%s,'pending',%s,0,0,%s)
            """,
            (user_id, business_id, operation_key, operation_key, total_reviews, force_reanalysis)
        )
        job_id = cursor.lastrowid
        conn.commit()
        return job_id, True
    except mysql.connector.IntegrityError as error:
        conn.rollback()
        if getattr(error, "errno", None) != 1062:
            raise
        cursor.execute(
            """
            SELECT id FROM analysis_jobs
            WHERE active_operation_key=%s
            AND status IN ('pending','processing')
            LIMIT 1
            """,
            (f"review_analysis:{business_id}",)
        )
        active = cursor.fetchone()
        if not active:
            raise
        return active["id"], False
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def create_consultant_job(user_id, business_id, max_attempts=3):
    operation_key = f"ai_consultant:{business_id}"
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            INSERT INTO analysis_jobs
                (user_id,business_id,job_type,operation_key,active_operation_key,
                 status,max_attempts,total_reviews)
            VALUES (%s,%s,'ai_consultant',%s,%s,'pending',%s,0)
            """,
            (user_id, business_id, operation_key, operation_key, max_attempts)
        )
        job_id = cursor.lastrowid
        conn.commit()
        return job_id, True
    except mysql.connector.IntegrityError as error:
        conn.rollback()
        if getattr(error, "errno", None) != 1062:
            raise
        cursor.execute(
            """
            SELECT id FROM analysis_jobs
            WHERE active_operation_key=%s
            AND status IN ('pending','processing') LIMIT 1
            """,
            (operation_key,)
        )
        active = cursor.fetchone()
        if not active:
            raise
        return active["id"], False
    finally:
        cursor.close()
        conn.close()


def get_job_status_for_user(job_id, user_id, is_admin=False):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        if is_admin:
            cursor.execute(
                """
                SELECT j.*
                FROM analysis_jobs j
                WHERE j.id=%s
                LIMIT 1
                """,
                (job_id,)
            )
        else:
            cursor.execute(
                """
                SELECT j.*
                FROM analysis_jobs j
                WHERE j.id=%s
                AND j.user_id=%s
                LIMIT 1
                """,
                (job_id, user_id)
            )

        job = cursor.fetchone()
        if not job:
            return None

        return {
            "job_id": job["id"],
            "business_id": job["business_id"],
            "status": job["status"],
            "total_reviews": job["total_reviews"] or 0,
            "processed_reviews": job["processed_reviews"] or 0,
            "failed_reviews": job["failed_reviews"] or 0,
            "error_message": job["error_message"],
            "report_id": job["latest_report_id"] if job["status"] == "completed" else None,
            "consultant_report_id": job.get("result_consultant_report_id") if job["status"] == "completed" else None,
            "job_type": job.get("job_type", "review_analysis"),
            "attempt_count": job.get("attempt_count", 0),
        }
    finally:
        cursor.close()
        conn.close()


def claim_next_job(worker_id, lease_seconds=DEFAULT_LEASE_SECONDS):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        conn.start_transaction()
        cursor.execute(
            """
            SELECT *
            FROM analysis_jobs
            WHERE status='pending'
            AND (next_attempt_at IS NULL OR next_attempt_at <= UTC_TIMESTAMP(6))
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """
        )
        job = cursor.fetchone()
        if not job:
            conn.commit()
            return None

        cursor.execute(
            """
            UPDATE analysis_jobs
            SET status='processing',
                worker_id=%s,
                started_at=COALESCE(started_at, UTC_TIMESTAMP(6)),
                heartbeat_at=UTC_TIMESTAMP(6),
                lease_expires_at=DATE_ADD(UTC_TIMESTAMP(6), INTERVAL %s SECOND),
                attempt_count=attempt_count+1,
                next_attempt_at=NULL,
                error_message=NULL
            WHERE id=%s
            AND status='pending'
            """,
            (worker_id, lease_seconds, job["id"])
        )
        conn.commit()
        job["status"] = "processing"
        job["worker_id"] = worker_id
        job["attempt_count"] = int(job.get("attempt_count", 0)) + 1
        return job
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def reset_stale_processing_jobs(timeout_minutes=30):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            UPDATE analysis_jobs
            SET status=CASE WHEN attempt_count>=max_attempts THEN 'failed' ELSE 'pending' END,
                completed_at=CASE WHEN attempt_count>=max_attempts THEN UTC_TIMESTAMP(6) ELSE completed_at END,
                active_operation_key=CASE WHEN attempt_count>=max_attempts THEN NULL ELSE active_operation_key END,
                worker_id=NULL,
                lease_expires_at=NULL,
                heartbeat_at=NULL,
                next_attempt_at=CASE WHEN attempt_count>=max_attempts THEN NULL ELSE UTC_TIMESTAMP(6) END,
                error_message='Worker lease expired before completing this job.'
            WHERE status='processing'
            AND (lease_expires_at < UTC_TIMESTAMP(6)
                 OR (lease_expires_at IS NULL AND started_at < DATE_SUB(NOW(), INTERVAL %s MINUTE)))
            """,
            (timeout_minutes,)
        )
        conn.commit()
        return cursor.rowcount
    finally:
        cursor.close()
        conn.close()


def get_active_job_for_business(business_id, job_type):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT * FROM analysis_jobs
            WHERE business_id=%s AND job_type=%s
            ORDER BY created_at DESC,id DESC LIMIT 1
            """,
            (business_id, job_type)
        )
        return cursor.fetchone()
    finally:
        cursor.close()
        conn.close()


def heartbeat_job(job_id, worker_id, lease_seconds=DEFAULT_LEASE_SECONDS):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            UPDATE analysis_jobs
            SET heartbeat_at=UTC_TIMESTAMP(6),
                lease_expires_at=DATE_ADD(UTC_TIMESTAMP(6), INTERVAL %s SECOND)
            WHERE id=%s AND status='processing' AND worker_id=%s
              AND lease_expires_at > UTC_TIMESTAMP(6)
            """,
            (lease_seconds, job_id, worker_id)
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        cursor.close()
        conn.close()


def confirm_job_ownership(job_id, worker_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT id FROM analysis_jobs
            WHERE id=%s AND status='processing' AND worker_id=%s
              AND lease_expires_at > UTC_TIMESTAMP(6)
            LIMIT 1
            """,
            (job_id, worker_id)
        )
        return cursor.fetchone() is not None
    finally:
        cursor.close()
        conn.close()


def complete_owned_job(job_id, worker_id, report_id=None, consultant_report_id=None):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            UPDATE analysis_jobs
            SET status='completed', completed_at=UTC_TIMESTAMP(6),
                latest_report_id=COALESCE(%s,latest_report_id),
                result_consultant_report_id=COALESCE(%s,result_consultant_report_id),
                active_operation_key=NULL, worker_id=NULL,
                lease_expires_at=NULL, heartbeat_at=NULL, error_message=NULL
            WHERE id=%s AND status='processing' AND worker_id=%s
              AND lease_expires_at > UTC_TIMESTAMP(6)
            """,
            (report_id, consultant_report_id, job_id, worker_id)
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        cursor.close()
        conn.close()


def fail_or_retry_owned_job(job, worker_id, error_message, retryable, delay_seconds=0):
    terminal = (not retryable) or int(job.get("attempt_count", 0)) >= int(job.get("max_attempts", 3))
    conn = get_connection()
    cursor = conn.cursor()
    try:
        if terminal:
            cursor.execute(
                """
                UPDATE analysis_jobs SET status='failed',completed_at=UTC_TIMESTAMP(6),
                    active_operation_key=NULL,worker_id=NULL,lease_expires_at=NULL,
                    heartbeat_at=NULL,error_message=%s
                WHERE id=%s AND status='processing' AND worker_id=%s
                """, (error_message[:500], job["id"], worker_id)
            )
        else:
            cursor.execute(
                """
                UPDATE analysis_jobs SET status='pending',worker_id=NULL,
                    lease_expires_at=NULL,heartbeat_at=NULL,
                    next_attempt_at=DATE_ADD(UTC_TIMESTAMP(6), INTERVAL %s SECOND),
                    error_message=%s
                WHERE id=%s AND status='processing' AND worker_id=%s
                """, (int(delay_seconds), error_message[:500], job["id"], worker_id)
            )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        cursor.close()
        conn.close()


def run_worker_forever(poll_seconds=5, batch_size=DEFAULT_BATCH_SIZE):
    while True:
        reset_stale_processing_jobs()
        job = claim_next_job()
        if not job:
            time.sleep(poll_seconds)
            continue

        process_analysis_job(job["id"], batch_size=batch_size)


def process_analysis_job(job_id, batch_size=DEFAULT_BATCH_SIZE, worker_id=None):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    ai_service = AIService()
    job = None

    try:
        cursor.execute(
            """
            SELECT *
            FROM analysis_jobs
            WHERE id=%s
            """,
            (job_id,)
        )
        job = cursor.fetchone()
        if not job:
            return True
        worker_id = worker_id or job.get("worker_id")

        cursor.execute(
            """
            SELECT
                business_name,
                business_type,
                city,
                state,
                country,
                use_reviewer_name,
                reply_tone,
                max_reply_words,
                auto_generate_replies_for_new_reviews
            FROM businesses
            WHERE id=%s
            """,
            (job["business_id"],)
        )
        business = cursor.fetchone() or {}

        while True:
            review_filter = """
                r.business_id=%s
                AND (
                    r.analysis_status IS NULL
                    OR r.analysis_status='pending'
                )
            """

            cursor.execute(
                f"""
                SELECT
                    r.id,
                    r.source,
                    r.rating,
                    r.reviewer_name,
                    r.review_text
                FROM reviews r
                WHERE {review_filter}
                ORDER BY r.id ASC
                LIMIT %s
                """,
                (job["business_id"], batch_size)
            )
            reviews = cursor.fetchall()

            if not reviews:
                break

            try:
                result = ai_service.analyze_review_batch(
                    reviews,
                    business=business,
                    settings=business
                )
                if worker_id and not confirm_job_ownership(job_id, worker_id):
                    conn.rollback()
                    return True
                log_ai_usage(cursor, job["user_id"], job["business_id"], result)
                _save_batch_results(
                    cursor,
                    reviews,
                    result.data.get("reviews", []),
                    auto_generate_replies=bool(
                        business.get("auto_generate_replies_for_new_reviews", True)
                    )
                )
                conn.commit()
            except AIServiceError as error:
                failed_result = error.result or AIResult(
                    data={},
                    provider="gemini",
                    model_name="gemini-2.5-flash",
                    operation_type="review_batch_analysis",
                    input_tokens=0,
                    output_tokens=0,
                    total_tokens=0,
                    estimated_cost=0.0,
                    request_status="failed",
                    response_time_ms=0,
                    error_message=str(error)[:1000],
                )
                log_ai_usage(cursor, job["user_id"], job["business_id"], failed_result)
                _mark_batch_failed(cursor, reviews, str(error))
                conn.commit()

            _refresh_job_progress(cursor, job_id, job["business_id"])
            conn.commit()

        if worker_id and not confirm_job_ownership(job_id, worker_id):
            return True
        report_id = _generate_report(cursor, ai_service, job)
        _refresh_job_progress(cursor, job_id, job["business_id"])
        try:
            if worker_id:
                cursor.execute(
                    """
                    UPDATE analysis_jobs
                    SET status='completed',completed_at=UTC_TIMESTAMP(6),
                        latest_report_id=%s,active_operation_key=NULL,
                        worker_id=NULL,lease_expires_at=NULL,heartbeat_at=NULL,
                        error_message=NULL
                    WHERE id=%s AND status='processing' AND worker_id=%s
                      AND lease_expires_at > UTC_TIMESTAMP(6)
                    """,
                    (report_id, job_id, worker_id),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    return True
                conn.commit()
            else:
                cursor.execute(
                    """UPDATE analysis_jobs SET status='completed',completed_at=NOW(),
                       latest_report_id=%s WHERE id=%s""", (report_id, job_id)
                )
                conn.commit()
        except Exception as persistence_error:
            _rollback_safely(conn, job_id)
            _log_analysis_persistence_error("completed", job_id, persistence_error)
            return False
    except Exception as error:
        _rollback_safely(conn, job_id)
        if isinstance(error, mysql.connector.Error):
            _log_analysis_infrastructure_error(job_id, error)
            return False
        else:
            try:
                if job and worker_id:
                    fail_or_retry_owned_job(
                        job, worker_id, _safe_analysis_error_message(error),
                        retryable=isinstance(error, AIServiceError),
                        delay_seconds=2 ** max(int(job.get("attempt_count", 1)) - 1, 0),
                    )
                else:
                    cursor.execute(
                        """UPDATE analysis_jobs SET status='failed',completed_at=NOW(),
                           error_message=%s WHERE id=%s""",
                        (_safe_analysis_error_message(error), job_id)
                    )
                    conn.commit()
            except Exception as persistence_error:
                _rollback_safely(conn, job_id)
                _log_analysis_persistence_error("failed", job_id, persistence_error)
                return False
        return True
    finally:
        cursor.close()
        conn.close()
    return True


def _rollback_safely(connection, job_id):
    try:
        connection.rollback()
    except Exception as rollback_error:
        _log_analysis_persistence_error("rollback", job_id, rollback_error)


def _safe_analysis_error_message(error):
    message = " ".join(str(error).split()) or error.__class__.__name__
    patterns = (
        r"(?i)(bearer\s+)[^\s,;]+",
        r"(?i)((?:access|refresh)[_-]?token\s*[=:]\s*)[^\s,;]+",
        r"(?i)(client[_-]?secret\s*[=:]\s*)[^\s,;]+",
        r"(?i)(password\s*[=:]\s*)[^\s,;]+",
        r"(?i)(authorization\s*[=:]\s*)[^\s,;]+",
        r"(?i)(mysql(?:\+\w+)?://)[^\s]+",
        r"(?i)(dsn\s*[=:]\s*)[^\s,;]+",
    )
    for pattern in patterns:
        message = re.sub(pattern, r"\1[REDACTED]", message)
    return message[:1000]


def _log_analysis_persistence_error(state, job_id, error):
    safe_exception = RuntimeError(_safe_analysis_error_message(error))
    logger.error(
        "Unable to persist AI analysis job state: job_id=%s state=%s "
        "component=ai_terminal_state",
        job_id,
        state,
        exc_info=(type(safe_exception), safe_exception, error.__traceback__),
    )


def _log_analysis_infrastructure_error(job_id, error):
    safe_exception = RuntimeError(_safe_analysis_error_message(error))
    logger.error(
        "AI analysis infrastructure failure: job_id=%s component=ai_job_execution",
        job_id,
        exc_info=(type(safe_exception), safe_exception, error.__traceback__),
    )


def _save_batch_results(cursor, reviews, analysis_rows, auto_generate_replies=True):
    rows_by_id = {
        int(row.get("review_id")): row
        for row in analysis_rows
        if row.get("review_id") is not None
    }

    for review in reviews:
        row = rows_by_id.get(int(review["id"]))
        if not row:
            cursor.execute(
                """
                UPDATE reviews
                SET analysis_status='failed',
                    analysis_error='AI response did not include this review.'
                WHERE id=%s
                """,
                (review["id"],)
            )
            continue

        suggested_reply = row.get("suggested_reply", "") if auto_generate_replies else None
        cursor.execute(
            """
            UPDATE reviews
            SET
                sentiment=%s,
                category=%s,
                complaint_praise_theme=%s,
                summary=%s,
                suggested_reply=%s,
                ai_reply=%s,
                reply_status='pending',
                reply_generated_at=CASE WHEN %s THEN NOW() ELSE reply_generated_at END,
                reply_error_message=NULL,
                confidence_score=%s,
                analysis_status='analyzed',
                analysis_error=NULL,
                analyzed_at=NOW()
            WHERE id=%s
            """,
            (
                row.get("sentiment", "Neutral"),
                row.get("category", "other"),
                row.get("theme", ""),
                row.get("summary", ""),
                suggested_reply,
                suggested_reply,
                auto_generate_replies,
                row.get("confidence_score", 0),
                review["id"],
            )
        )


def _mark_batch_failed(cursor, reviews, error_message):
    for review in reviews:
        cursor.execute(
            """
            UPDATE reviews
            SET analysis_status='failed',
                analysis_error=%s
            WHERE id=%s
            """,
            (error_message[:1000], review["id"])
        )


def _refresh_job_progress(cursor, job_id, business_id):
    cursor.execute(
        """
        SELECT
            COUNT(*) AS total_reviews,
            SUM(CASE WHEN analysis_status='analyzed' THEN 1 ELSE 0 END) AS processed_reviews,
            SUM(CASE WHEN analysis_status='failed' THEN 1 ELSE 0 END) AS failed_reviews
        FROM reviews
        WHERE business_id=%s
        """,
        (business_id,)
    )
    counts = cursor.fetchone()
    cursor.execute(
        """
        UPDATE analysis_jobs
        SET total_reviews=%s,
            processed_reviews=%s,
            failed_reviews=%s
        WHERE id=%s
        """,
        (
            counts["total_reviews"] or 0,
            counts["processed_reviews"] or 0,
            counts["failed_reviews"] or 0,
            job_id,
        )
    )


def _generate_report(cursor, ai_service, job):
    cursor.execute(
        """
        SELECT
            id,
            rating,
            sentiment,
            category,
            complaint_praise_theme,
            summary
        FROM reviews
        WHERE business_id=%s
        AND analysis_status='analyzed'
        ORDER BY analyzed_at DESC
        LIMIT 500
        """,
        (job["business_id"],)
    )
    analyzed_reviews = cursor.fetchall()
    if not analyzed_reviews:
        return None

    try:
        report = ai_service.generate_business_report(analyzed_reviews)
        log_ai_usage(cursor, job["user_id"], job["business_id"], report)
        data = report.data
    except AIServiceError as error:
        failed_result = error.result or AIResult(
            data={},
            provider="gemini",
            model_name="gemini-2.5-flash",
            operation_type="business_report",
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            estimated_cost=0.0,
            request_status="failed",
            response_time_ms=0,
            error_message=str(error)[:1000],
        )
        log_ai_usage(cursor, job["user_id"], job["business_id"], failed_result)
        raise

    cursor.execute(
        """
        INSERT INTO reports
        (
            business_id,
            summary,
            top_complaints,
            top_praises,
            recommendations,
            sentiment_score,
            review_count
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            job["business_id"],
            data.get("summary", ""),
            json.dumps(data.get("top_complaints", [])),
            json.dumps(data.get("top_praises", [])),
            json.dumps(data.get("recommendations", [])),
            data.get("sentiment_score", 0),
            len(analyzed_reviews),
        )
    )
    return cursor.lastrowid
