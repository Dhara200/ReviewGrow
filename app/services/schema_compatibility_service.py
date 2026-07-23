import threading

from app.config import Config
from app.services import database_service


class SchemaCompatibilityError(RuntimeError):
    """Sanitized startup failure for an unavailable or incompatible database."""


REQUIRED_SCHEMA = {
    "users": {"id", "email", "password_hash", "role"},
    "businesses": {"id", "user_id"},
    "reviews": {"id", "business_id", "review_text", "analysis_status"},
    "reports": {"id", "business_id"},
    "subscriptions": {"id", "user_id", "status", "subscription_end_date"},
    "payments": {
        "id",
        "user_id",
        "payment_status",
        "razorpay_order_id",
        "razorpay_payment_id",
    },
    "rate_limit_counters": {
        "scope",
        "key_hash",
        "window_started_at",
        "attempt_count",
        "blocked_until",
    },
    "analysis_jobs": {
        "id",
        "user_id",
        "business_id",
        "status",
        "job_type",
        "worker_id",
        "lease_expires_at",
    },
    "google_review_sync_jobs": {
        "id",
        "user_id",
        "business_id",
        "status",
        "worker_id",
        "lease_expires_at",
    },
    "google_business_connections": {
        "id",
        "user_id",
        "business_id",
        "access_token",
        "refresh_token",
    },
}

_validation_lock = threading.Lock()
_validated = False


def validate_runtime_schema():
    """Verify the minimum runtime schema once per process using metadata only."""
    global _validated
    if _validated:
        return

    with _validation_lock:
        if _validated:
            return
        _validate_runtime_schema_uncached()
        _validated = True


def _validate_runtime_schema_uncached():
    connection = None
    cursor = None
    try:
        connection = database_service.get_connection()
        cursor = connection.cursor(dictionary=True)
        table_names = tuple(sorted(REQUIRED_SCHEMA))
        placeholders = ",".join(["%s"] * len(table_names))
        cursor.execute(
            f"""
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema=%s
            AND table_name IN ({placeholders})
            """,
            (Config.DB_NAME, *table_names),
        )
        rows = cursor.fetchall()
    except Exception:
        raise SchemaCompatibilityError(
            "Database unavailable during schema compatibility check."
        ) from None
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    present = {table: set() for table in REQUIRED_SCHEMA}
    for row in rows:
        if isinstance(row, dict):
            table_name = row.get("table_name")
            column_name = row.get("column_name")
        else:
            table_name, column_name = row
        if table_name in present and column_name:
            present[table_name].add(column_name)

    missing = sorted(
        f"{table}.{column}"
        for table, columns in REQUIRED_SCHEMA.items()
        for column in columns - present[table]
    )
    if missing:
        summary = ", ".join(missing[:8])
        if len(missing) > 8:
            summary += f", and {len(missing) - 8} more"
        raise SchemaCompatibilityError(
            "Database schema is incompatible; apply pending migrations. "
            f"Missing required metadata: {summary}."
        )


def _reset_validation_cache_for_tests():
    global _validated
    with _validation_lock:
        _validated = False
