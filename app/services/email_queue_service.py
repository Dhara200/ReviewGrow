"""Database-backed transactional email queue."""

import json
from datetime import datetime, timedelta, timezone

import mysql.connector

from app.config import Config
from app.services.database_service import get_connection
from app.utils.datetime_utils import format_datetime_ist


WELCOME_SUBJECT = "Welcome to ReviewGrow — your account is ready"
RETRY_DELAYS_SECONDS = (60, 300, 900, 3600, 21600)
SUBSCRIPTION_CONFIRMATION_SUBJECT = (
    "Payment confirmed — your ReviewGrow Premium plan is active"
)


def enqueue_email(recipient_email, email_type, template_name, template_data,
                  *, user_id=None, priority=100, max_attempts=6,
                  deduplication_key=None):
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO email_jobs
                (user_id,recipient_email,email_type,template_name,template_data,
                 priority,status,attempt_count,max_attempts,next_attempt_at,
                 deduplication_key,created_at,updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,'pending',0,%s,UTC_TIMESTAMP(6),%s,
                    UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))
            """,
            (user_id, recipient_email, email_type, template_name,
             json.dumps(template_data, ensure_ascii=False), int(priority),
             int(max_attempts), deduplication_key),
        )
        job_id = cursor.lastrowid
        connection.commit()
        return job_id
    except mysql.connector.IntegrityError as error:
        connection.rollback()
        if error.errno != 1062 or not deduplication_key:
            raise
        cursor.execute(
            "SELECT id FROM email_jobs WHERE deduplication_key=%s LIMIT 1",
            (deduplication_key,),
        )
        row = cursor.fetchone()
        return row[0] if row else None
    finally:
        cursor.close()
        connection.close()


def enqueue_welcome_email(user):
    user_id = user["id"] if isinstance(user, dict) else user.id
    email = user["email"] if isinstance(user, dict) else user.email
    name = (user.get("name") if isinstance(user, dict) else getattr(user, "name", "")) or ""
    return enqueue_email(
        email, "welcome", "welcome",
        {
            "subject": WELCOME_SUBJECT,
            "display_name": name,
            "login_url": f"{Config.APP_BASE_URL}/login-page",
            "support_email": Config.SES_REPLY_TO_EMAIL or "founder@reviewgrow.in",
        },
        user_id=user_id, priority=50, deduplication_key=f"welcome:{user_id}",
    )


def enqueue_subscription_confirmation_email(details):
    payment_id = str(details["razorpay_payment_id"])
    amount_paise = int(details["amount_paise"])
    currency = str(details["currency"])
    symbol = "₹" if currency == "INR" else f"{currency} "
    return enqueue_email(
        details["email"], "subscription_confirmation", "subscription_confirmation",
        {
            "subject": SUBSCRIPTION_CONFIRMATION_SUBJECT,
            "customer_name": details.get("name") or "",
            "plan_name": details["plan_name"],
            "amount_display": f"{symbol}{amount_paise / 100:,.2f}",
            "currency": currency,
            "razorpay_payment_id": payment_id,
            "razorpay_order_id": str(details["razorpay_order_id"]),
            "subscription_start_date": format_datetime_ist(
                details.get("subscription_start_date")
            ),
            "subscription_end_date": format_datetime_ist(
                details.get("subscription_end_date")
            ),
            "dashboard_url": f"{Config.APP_BASE_URL}/my-businesses",
            "support_email": Config.SES_REPLY_TO_EMAIL or "founder@reviewgrow.in",
        },
        user_id=int(details["user_id"]), priority=20,
        deduplication_key=f"subscription_confirmation:{payment_id}",
    )


def claim_pending_email_jobs(limit=1):
    connection = get_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        connection.start_transaction()
        cursor.execute(
            """
            SELECT * FROM email_jobs
            WHERE status='pending' AND next_attempt_at<=UTC_TIMESTAMP(6)
            ORDER BY priority ASC, created_at ASC
            LIMIT %s FOR UPDATE SKIP LOCKED
            """, (max(1, int(limit)),),
        )
        jobs = cursor.fetchall()
        if jobs:
            ids = [job["id"] for job in jobs]
            placeholders = ",".join(["%s"] * len(ids))
            cursor.execute(
                f"""UPDATE email_jobs SET status='processing',
                    attempt_count=attempt_count+1,
                    processing_started_at=UTC_TIMESTAMP(6),updated_at=UTC_TIMESTAMP(6)
                    WHERE id IN ({placeholders}) AND status='pending'""", tuple(ids),
            )
            for job in jobs:
                job["status"] = "processing"
                job["attempt_count"] = int(job.get("attempt_count") or 0) + 1
                if isinstance(job.get("template_data"), str):
                    job["template_data"] = json.loads(job["template_data"])
        connection.commit()
        return jobs
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def mark_email_sent(job_id, message_id):
    return _terminal_update(
        """UPDATE email_jobs SET status='sent',ses_message_id=%s,
           sent_at=UTC_TIMESTAMP(6),processing_started_at=NULL,last_error=NULL,
           template_data=IF(email_type='login_otp',
             JSON_OBJECT('redacted',TRUE,'challenge_id',JSON_EXTRACT(template_data,'$.challenge_id')),
             template_data),
           updated_at=UTC_TIMESTAMP(6) WHERE id=%s AND status='processing'""",
        (message_id, job_id),
    )


def mark_email_failed(job, error_message, *, retryable):
    attempt_count = int(job.get("attempt_count") or 0)
    max_attempts = int(job.get("max_attempts") or 6)
    safe_error = " ".join(str(error_message).split())[:500]
    if retryable and attempt_count < max_attempts:
        delay_index = min(max(attempt_count - 1, 0), len(RETRY_DELAYS_SECONDS) - 1)
        next_attempt = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
            seconds=RETRY_DELAYS_SECONDS[delay_index]
        )
        return _terminal_update(
            """UPDATE email_jobs SET status='pending',next_attempt_at=%s,
               last_error=%s,processing_started_at=NULL,updated_at=UTC_TIMESTAMP(6)
               WHERE id=%s AND status='processing'""",
            (next_attempt, safe_error, job["id"]),
        )
    return _terminal_update(
        """UPDATE email_jobs SET status='failed',last_error=%s,
           template_data=IF(email_type='login_otp',
             JSON_OBJECT('redacted',TRUE,'challenge_id',JSON_EXTRACT(template_data,'$.challenge_id')),
             template_data),
           processing_started_at=NULL,updated_at=UTC_TIMESTAMP(6)
           WHERE id=%s AND status='processing'""",
        (safe_error, job["id"]),
    )


def mark_email_cancelled(job_id, reason="Email job cancelled"):
    return _terminal_update(
        """UPDATE email_jobs SET status='cancelled',last_error=%s,
           template_data=IF(email_type='login_otp',JSON_OBJECT('redacted',TRUE),template_data),
           processing_started_at=NULL,updated_at=UTC_TIMESTAMP(6)
           WHERE id=%s AND status='processing'""",
        (" ".join(str(reason).split())[:500], job_id),
    )


def recover_stale_email_jobs(timeout_minutes=15):
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """UPDATE email_jobs SET
                status=IF(attempt_count<max_attempts,'pending','failed'),
                next_attempt_at=IF(attempt_count<max_attempts,UTC_TIMESTAMP(6),next_attempt_at),
                last_error='Recovered after processing timeout',
                processing_started_at=NULL,updated_at=UTC_TIMESTAMP(6)
               WHERE status='processing'
                 AND processing_started_at < UTC_TIMESTAMP(6) - INTERVAL %s MINUTE""",
            (max(1, int(timeout_minutes)),),
        )
        count = cursor.rowcount
        connection.commit()
        return count
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def _terminal_update(query, params):
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(query, params)
        changed = cursor.rowcount == 1
        connection.commit()
        return changed
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()
