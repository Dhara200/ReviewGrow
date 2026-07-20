import re

from app.config import Config
from app.services.database_service import get_connection


SYNC_JOB_STATUSES = frozenset({"pending", "processing", "completed", "failed"})
SYNC_QUEUE_DATE_RANGES = frozenset({"today", "24h", "7d", "all"})
RECENT_JOB_LIMIT = 50
ERROR_DISPLAY_LIMIT = 160


class AdminSyncQueueService:
    """Read-only monitoring queries for the Google review synchronization queue."""

    def __init__(self, connection_factory=get_connection):
        self._connection_factory = connection_factory

    def get_sync_queue_summary(self):
        connection = self._connection_factory()
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT
                    COALESCE(SUM(status='pending'), 0) AS pending_jobs,
                    COALESCE(SUM(status='processing'), 0) AS running_jobs,
                    COALESCE(SUM(
                        status='completed' AND DATE(completed_at)=UTC_DATE()
                    ), 0) AS completed_today,
                    COALESCE(SUM(
                        status='failed' AND DATE(completed_at)=UTC_DATE()
                    ), 0) AS failed_today,
                    AVG(CASE
                        WHEN status='completed'
                         AND completed_at >= DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 24 HOUR)
                         AND started_at IS NOT NULL
                         AND completed_at >= started_at
                        THEN TIMESTAMPDIFF(MICROSECOND, started_at, completed_at) / 1000000
                    END) AS average_sync_seconds,
                    TIMESTAMPDIFF(
                        SECOND,
                        MIN(CASE WHEN status='pending' THEN created_at END),
                        UTC_TIMESTAMP(6)
                    ) AS oldest_pending_seconds
                FROM google_review_sync_jobs
                """
            )
            row = cursor.fetchone() or {}
            return {
                "pending_jobs": int(row.get("pending_jobs") or 0),
                "running_jobs": int(row.get("running_jobs") or 0),
                "completed_today": int(row.get("completed_today") or 0),
                "failed_today": int(row.get("failed_today") or 0),
                "average_sync_seconds": _optional_float(
                    row.get("average_sync_seconds")
                ),
                "oldest_pending_seconds": _optional_int(
                    row.get("oldest_pending_seconds")
                ),
            }
        finally:
            cursor.close()
            connection.close()

    def get_recent_sync_jobs(
        self,
        status=None,
        business_id=None,
        date_range="24h",
        limit=RECENT_JOB_LIMIT,
    ):
        status, business_id, date_range = normalize_sync_queue_filters(
            status, business_id, date_range
        )
        limit = min(max(int(limit), 1), RECENT_JOB_LIMIT)
        clauses = []
        params = []
        if status:
            clauses.append("j.status=%s")
            params.append(status)
        if business_id:
            clauses.append("j.business_id=%s")
            params.append(business_id)
        date_clause = _date_filter_sql(date_range)
        if date_clause:
            clauses.append(date_clause)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)

        connection = self._connection_factory()
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                f"""
                SELECT
                    j.id,
                    j.business_id,
                    b.business_name,
                    j.user_id,
                    u.name AS user_name,
                    u.email AS user_email,
                    j.status,
                    j.worker_id,
                    j.created_at,
                    j.started_at,
                    j.completed_at,
                    j.heartbeat_at,
                    j.lease_expires_at,
                    j.error_message,
                    CASE
                        WHEN j.started_at IS NOT NULL AND j.completed_at IS NOT NULL
                         AND j.completed_at >= j.started_at
                        THEN TIMESTAMPDIFF(SECOND, j.started_at, j.completed_at)
                        WHEN j.status='processing' AND j.started_at IS NOT NULL
                        THEN TIMESTAMPDIFF(SECOND, j.started_at, UTC_TIMESTAMP(6))
                        ELSE NULL
                    END AS duration_seconds
                FROM google_review_sync_jobs j
                JOIN businesses b ON b.id=j.business_id
                JOIN users u ON u.id=j.user_id
                {where_sql}
                ORDER BY j.created_at DESC, j.id DESC
                LIMIT %s
                """,
                tuple(params),
            )
            jobs = cursor.fetchall()
            for job in jobs:
                job["attempt_count"] = None
                job["safe_error"] = sanitize_sync_job_error(job.pop("error_message", None))
            return jobs
        finally:
            cursor.close()
            connection.close()

    def get_sync_queue_health(self, summary=None):
        summary = summary or self.get_sync_queue_summary()
        stale_threshold_seconds = max(
            1,
            Config.GOOGLE_REVIEW_SYNC_HEARTBEAT_SECONDS * 2,
        )
        connection = self._connection_factory()
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT
                    COALESCE(SUM(
                        status='processing'
                        AND lease_expires_at IS NOT NULL
                        AND lease_expires_at < UTC_TIMESTAMP(6)
                    ), 0) AS expired_leases,
                    COALESCE(SUM(
                        status='processing'
                        AND (
                            heartbeat_at IS NULL
                            OR heartbeat_at < DATE_SUB(
                                UTC_TIMESTAMP(6), INTERVAL %s SECOND
                            )
                        )
                    ), 0) AS stale_heartbeats
                FROM google_review_sync_jobs
                """,
                (stale_threshold_seconds,),
            )
            row = cursor.fetchone() or {}
        finally:
            cursor.close()
            connection.close()

        expired_leases = int(row.get("expired_leases") or 0)
        stale_heartbeats = int(row.get("stale_heartbeats") or 0)
        warnings = []
        if summary["pending_jobs"] >= 5:
            warnings.append(f"Queue backlog: {summary['pending_jobs']} jobs are pending.")
        if (
            summary["oldest_pending_seconds"] is not None
            and summary["oldest_pending_seconds"] > 120
        ):
            warnings.append("The oldest pending job has waited more than 2 minutes.")
        if expired_leases:
            warnings.append(f"{expired_leases} processing job(s) have an expired lease.")
        if stale_heartbeats:
            warnings.append(
                f"{stale_heartbeats} processing job(s) have a stale or missing heartbeat."
            )
        if summary["failed_today"] >= 3:
            warnings.append(f"Failure spike: {summary['failed_today']} jobs failed today.")
        return {
            "warnings": warnings,
            "expired_leases": expired_leases,
            "stale_heartbeats": stale_heartbeats,
            "stale_threshold_seconds": stale_threshold_seconds,
        }

    def get_business_options(self):
        connection = self._connection_factory()
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT DISTINCT b.id, b.business_name
                FROM businesses b
                JOIN google_review_sync_jobs j ON j.business_id=b.id
                ORDER BY b.business_name ASC, b.id ASC
                """
            )
            return cursor.fetchall()
        finally:
            cursor.close()
            connection.close()

    def get_recent_ai_jobs(self, limit=RECENT_JOB_LIMIT):
        connection = self._connection_factory()
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT j.id,j.business_id,b.business_name,j.user_id,
                    u.name AS user_name,j.job_type,j.status,j.attempt_count,
                    j.worker_id,j.created_at,j.started_at,j.completed_at,
                    j.lease_expires_at,j.error_message,
                    CASE
                        WHEN j.status<>'processing' THEN NULL
                        WHEN j.lease_expires_at > UTC_TIMESTAMP(6) THEN 'healthy'
                        ELSE 'expired'
                    END AS lease_health
                FROM analysis_jobs j
                JOIN businesses b ON b.id=j.business_id
                JOIN users u ON u.id=j.user_id
                ORDER BY j.created_at DESC,j.id DESC LIMIT %s
                """, (min(max(int(limit), 1), RECENT_JOB_LIMIT),)
            )
            jobs = cursor.fetchall()
            for job in jobs:
                job["safe_error"] = sanitize_sync_job_error(
                    job.pop("error_message", None)
                )
            return jobs
        finally:
            cursor.close()
            connection.close()


def normalize_sync_queue_filters(status=None, business_id=None, date_range="24h"):
    normalized_status = status if status in SYNC_JOB_STATUSES else None
    try:
        normalized_business_id = int(business_id) if business_id else None
    except (TypeError, ValueError):
        normalized_business_id = None
    if normalized_business_id is not None and normalized_business_id <= 0:
        normalized_business_id = None
    normalized_date_range = (
        date_range if date_range in SYNC_QUEUE_DATE_RANGES else "24h"
    )
    return normalized_status, normalized_business_id, normalized_date_range


def sanitize_sync_job_error(error_message):
    if not error_message:
        return ""
    message = " ".join(str(error_message).split())
    patterns = (
        r"(?i)(bearer\s+)[^\s,;]+",
        r"(?i)((?:access|refresh)[_-]?token\s*[=:]\s*)[^\s,;]+",
        r"(?i)(client[_-]?secret\s*[=:]\s*)[^\s,;]+",
        r"(?i)(password\s*[=:]\s*)[^\s,;]+",
        r"(?i)(authorization\s*[=:]\s*)[^\s,;]+",
        r"(?i)(mysql(?:\+\w+)?://)[^\s]+",
    )
    for pattern in patterns:
        message = re.sub(pattern, r"\1[REDACTED]", message)
    if len(message) > ERROR_DISPLAY_LIMIT:
        return f"{message[:ERROR_DISPLAY_LIMIT - 1]}…"
    return message


def format_duration(seconds, empty_label="N/A"):
    if seconds is None:
        return empty_label
    seconds = max(0, int(round(float(seconds))))
    if seconds < 60:
        return f"{seconds}s"
    minutes, remaining_seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remaining_seconds}s"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours}h {remaining_minutes}m"


def _date_filter_sql(date_range):
    if date_range == "today":
        return "j.created_at >= UTC_DATE()"
    if date_range == "24h":
        return "j.created_at >= DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 24 HOUR)"
    if date_range == "7d":
        return "j.created_at >= DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 7 DAY)"
    return ""


def _optional_int(value):
    return None if value is None else int(value)


def _optional_float(value):
    return None if value is None else float(value)
