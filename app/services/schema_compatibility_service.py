import logging
import threading
from collections.abc import Mapping

from app.services import database_service


logger = logging.getLogger(__name__)


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
    "ai_usage_logs": {
        "user_id",
        "request_status",
        "created_at",
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
        logger.info(
            "Schema compatibility validation: stage=connection "
            "active_database_selected=unknown"
        )
        cursor.execute("SELECT DATABASE() AS active_database")
        active_database = _row_values(
            cursor.fetchone(),
            ("active_database",),
        )[0]
        if not isinstance(active_database, str) or not active_database.strip():
            raise SchemaCompatibilityError(
                "Database connection has no active schema selected."
            )
        logger.info(
            "Schema compatibility validation: stage=database_selection "
            "active_database_selected=true"
        )

        table_names = tuple(sorted(REQUIRED_SCHEMA))
        placeholders = ",".join(["%s"] * len(table_names))
        cursor.execute(
            f"""
            SELECT TABLE_NAME AS table_name, COLUMN_NAME AS column_name
            FROM information_schema.columns
            WHERE TABLE_SCHEMA=%s
            AND TABLE_NAME IN ({placeholders})
            """,
            (active_database, *table_names),
        )
        rows = cursor.fetchall()
        logger.info(
            "Schema compatibility validation: stage=metadata "
            "metadata_row_count=%d",
            len(rows),
        )
    except SchemaCompatibilityError:
        raise
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

    missing = _missing_required_metadata(rows)
    if missing:
        summary = ", ".join(missing[:8])
        if len(missing) > 8:
            summary += f", and {len(missing) - 8} more"
        raise SchemaCompatibilityError(
            "Database schema is incompatible; apply pending migrations. "
            f"Missing required metadata: {summary}."
        )


def _row_values(row, expected_names):
    if isinstance(row, Mapping):
        normalized = {
            str(key).casefold(): value
            for key, value in row.items()
        }
        try:
            return tuple(normalized[name.casefold()] for name in expected_names)
        except KeyError:
            raise SchemaCompatibilityError(
                "Database metadata response format is incompatible."
            ) from None

    if isinstance(row, (tuple, list)) and len(row) >= len(expected_names):
        return tuple(row[:len(expected_names)])

    raise SchemaCompatibilityError(
        "Database metadata response format is incompatible."
    )


def _missing_required_metadata(rows):
    present = {table: set() for table in REQUIRED_SCHEMA}
    for row in rows:
        table_name, column_name = _row_values(
            row,
            ("table_name", "column_name"),
        )
        if table_name in present and column_name:
            present[table_name].add(column_name)

    return sorted(
        f"{table}.{column}"
        for table, columns in REQUIRED_SCHEMA.items()
        for column in columns - present[table]
    )


def _reset_validation_cache_for_tests():
    global _validated
    with _validation_lock:
        _validated = False
