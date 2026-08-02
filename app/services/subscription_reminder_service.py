"""Idempotent five-day subscription renewal reminder generation."""

import logging
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parseaddr
from zoneinfo import ZoneInfo

from app.config import Config
from app.services.database_service import get_connection
from app.services.email_queue_service import enqueue_renewal_reminder_email
from app.utils.datetime_utils import format_datetime_ist


logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
PAID_PLAN_KEY = "starter"


def reminder_window_utc(now=None, days=None):
    now_utc = _aware_utc(now or datetime.now(timezone.utc))
    reminder_days = _valid_days(days)
    target_date = now_utc.astimezone(IST).date() + timedelta(days=reminder_days)
    start = datetime.combine(target_date, time.min, IST).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    return target_date, start.replace(tzinfo=None), end.replace(tzinfo=None)


def generate_subscription_renewal_reminders(*, dry_run=False, now=None):
    target_date, start_utc, end_utc = reminder_window_utc(now)
    summary = {
        "target_date": target_date.isoformat(), "scanned": 0, "eligible": 0,
        "queued": 0, "duplicates": 0, "skipped": 0, "errors": 0,
        "disabled": not Config.SUBSCRIPTION_RENEWAL_REMINDER_ENABLED,
        "dry_run": bool(dry_run),
    }
    if summary["disabled"]:
        return summary

    connection = get_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT s.id AS subscription_id,s.user_id,s.plan_name,s.status,
                   s.subscription_end_date,u.name,u.email,u.role
            FROM subscriptions s JOIN users u ON u.id=s.user_id
            WHERE s.status='active' AND s.plan_name=%s
              AND s.subscription_end_date >= %s AND s.subscription_end_date < %s
              AND NOT EXISTS (
                SELECT 1 FROM subscriptions newer
                WHERE newer.user_id=s.user_id
                  AND (newer.created_at>s.created_at OR
                      (newer.created_at=s.created_at AND newer.id>s.id)))
            ORDER BY s.id ASC
            """,
            (PAID_PLAN_KEY, start_utc, end_utc),
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()
        connection.close()

    summary["scanned"] = len(rows)
    for row in rows:
        if row.get("role") == "admin" or not _valid_email(row.get("email")):
            summary["skipped"] += 1
            continue
        summary["eligible"] += 1
        if dry_run:
            continue
        try:
            end_value = row["subscription_end_date"]
            job_id, created = enqueue_renewal_reminder_email({
                "user_id": row["user_id"], "email": row["email"],
                "plan_name": "ReviewGrow Premium",
                "subscription_end_date": end_value,
                "subscription_end_date_ist": target_date.isoformat(),
                "subscription_end_date_display": format_datetime_ist(end_value),
                "days_remaining": Config.SUBSCRIPTION_RENEWAL_REMINDER_DAYS,
            })
            summary["queued" if created else "duplicates"] += 1
            logger.info("Renewal reminder queue result: user_id=%s job_id=%s created=%s",
                        row["user_id"], job_id, created)
        except Exception:
            summary["errors"] += 1
            logger.exception("Renewal reminder queue failed: user_id=%s", row["user_id"])
    return summary


def validate_renewal_reminder_job(job, now=None):
    """Return (eligible, cancellation_reason, current customer name)."""
    if not Config.SUBSCRIPTION_RENEWAL_REMINDER_ENABLED:
        return False, "renewal_reminders_disabled", ""
    data = job.get("template_data") or {}
    expected_raw = data.get("expected_subscription_end")
    try:
        expected = datetime.fromisoformat(expected_raw)
    except (TypeError, ValueError):
        return False, "invalid_expected_subscription_end", ""

    connection = get_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """SELECT s.subscription_end_date,s.status,s.plan_name,u.name,u.email,u.role
               FROM subscriptions s JOIN users u ON u.id=s.user_id
               WHERE s.user_id=%s ORDER BY s.created_at DESC,s.id DESC LIMIT 1""",
            (int(job["user_id"]),),
        )
        row = cursor.fetchone()
    finally:
        cursor.close()
        connection.close()
    if not row or row.get("status") != "active" or row.get("plan_name") != PAID_PLAN_KEY:
        return False, "subscription_inactive", ""
    if row.get("role") == "admin" or row.get("email") != job.get("recipient_email"):
        return False, "subscription_ineligible", ""
    actual = row.get("subscription_end_date")
    if actual != expected:
        return False, "subscription_renewed", ""
    target, _start, _end = reminder_window_utc(now)
    if actual.replace(tzinfo=timezone.utc).astimezone(IST).date() != target:
        return False, "reminder_window_passed", ""
    return True, None, row.get("name") or ""


def _valid_days(value):
    try:
        value = int(Config.SUBSCRIPTION_RENEWAL_REMINDER_DAYS if value is None else value)
    except (TypeError, ValueError):
        return 5
    return value if 1 <= value <= 30 else 5


def _aware_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _valid_email(value):
    if not isinstance(value, str) or len(value) > 254:
        return False
    parsed = parseaddr(value)[1]
    return parsed == value and "@" in parsed and not parsed.startswith("@")
