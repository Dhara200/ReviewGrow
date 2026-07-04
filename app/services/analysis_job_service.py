import json
import time

from app.services.ai_service import AIResult, AIService, AIServiceError, log_ai_usage
from app.services.database_service import get_connection


ACTIVE_JOB_STATUSES = ("pending", "processing")
DEFAULT_BATCH_SIZE = 25


def create_analysis_job(user_id, business_id, force_reanalysis=False):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT id, status
            FROM analysis_jobs
            WHERE business_id=%s
            AND status IN ('pending', 'processing')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (business_id,)
        )
        active_job = cursor.fetchone()
        if active_job:
            return active_job["id"], False

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
                status,
                total_reviews,
                processed_reviews,
                failed_reviews,
                force_reanalysis
            )
            VALUES (%s,%s,'pending',%s,0,0,%s)
            """,
            (user_id, business_id, total_reviews, force_reanalysis)
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
        }
    finally:
        cursor.close()
        conn.close()


def claim_next_job():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        conn.start_transaction()
        cursor.execute(
            """
            SELECT *
            FROM analysis_jobs
            WHERE status='pending'
            ORDER BY created_at ASC
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
                started_at=COALESCE(started_at, NOW()),
                error_message=NULL
            WHERE id=%s
            """,
            (job["id"],)
        )
        conn.commit()
        job["status"] = "processing"
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
            SET status='pending',
                error_message='Worker stopped before completing this job.'
            WHERE status='processing'
            AND started_at < DATE_SUB(NOW(), INTERVAL %s MINUTE)
            """,
            (timeout_minutes,)
        )
        conn.commit()
        return cursor.rowcount
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


def process_analysis_job(job_id, batch_size=DEFAULT_BATCH_SIZE):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    ai_service = AIService()

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
            return

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
                result = ai_service.analyze_review_batch(reviews)
                log_ai_usage(cursor, job["user_id"], job["business_id"], result)
                _save_batch_results(cursor, reviews, result.data.get("reviews", []))
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

        report_id = _generate_report(cursor, ai_service, job)
        _refresh_job_progress(cursor, job_id, job["business_id"])
        cursor.execute(
            """
            UPDATE analysis_jobs
            SET status='completed',
                completed_at=NOW(),
                latest_report_id=%s
            WHERE id=%s
            """,
            (report_id, job_id)
        )
        conn.commit()
    except Exception as error:
        conn.rollback()
        cursor.execute(
            """
            UPDATE analysis_jobs
            SET status='failed',
                completed_at=NOW(),
                error_message=%s
            WHERE id=%s
            """,
            (str(error)[:1000], job_id)
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def _save_batch_results(cursor, reviews, analysis_rows):
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
                row.get("suggested_reply", ""),
                row.get("suggested_reply", ""),
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
