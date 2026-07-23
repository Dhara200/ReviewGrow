from dataclasses import dataclass
from collections.abc import Mapping

from app.services.database_service import get_connection
from app.services.limiter_service import LimiterService


AI_REVIEW_TEXT_MAX_LENGTH = 5_000
AI_RATE_WINDOW_SECONDS = 600
AI_RATE_BLOCK_SECONDS = 600
AI_USER_REQUEST_LIMIT = 10
AI_IP_REQUEST_LIMIT = 20


class AISecurityUnavailable(RuntimeError):
    pass


class AIQuotaExceeded(RuntimeError):
    pass


class AIRequestInProgress(RuntimeError):
    pass


@dataclass
class AIQuotaSlot:
    connection: object
    cursor: object
    lock_name: str
    used_requests: int
    request_limit: int

    def close(self):
        try:
            try:
                self.cursor.execute("SELECT RELEASE_LOCK(%s)", (self.lock_name,))
            except Exception:
                pass
        finally:
            try:
                self.cursor.close()
            finally:
                self.connection.close()


def validate_sync_ai_security_config(app):
    value = app.config.get("MAX_AI_REQUESTS_PER_MONTH")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeError(
            "MAX_AI_REQUESTS_PER_MONTH must be a positive integer."
        )


def validate_review_text(value):
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized:
        raise ValueError("Enter a customer review before using AI.")
    if len(normalized) > AI_REVIEW_TEXT_MAX_LENGTH:
        raise ValueError(
            f"Customer review must be no more than "
            f"{AI_REVIEW_TEXT_MAX_LENGTH:,} characters."
        )
    if any(
        ord(character) < 32 and character not in {"\n", "\r", "\t"}
        for character in normalized
    ):
        raise ValueError("Customer review contains unsupported control characters.")
    return normalized


def consume_ai_rate_limits(user_id, client_ip, limiter=None):
    active_limiter = limiter or LimiterService()
    try:
        ip_status = active_limiter.record_failure(
            "ai_ip",
            client_ip,
            threshold=AI_IP_REQUEST_LIMIT + 1,
            window_seconds=AI_RATE_WINDOW_SECONDS,
            block_seconds=AI_RATE_BLOCK_SECONDS,
        )
        if ip_status.blocked:
            return "ip", ip_status

        user_status = active_limiter.record_failure(
            "ai_user",
            f"user:{int(user_id)}",
            threshold=AI_USER_REQUEST_LIMIT + 1,
            window_seconds=AI_RATE_WINDOW_SECONDS,
            block_seconds=AI_RATE_BLOCK_SECONDS,
        )
        if user_status.blocked:
            return "user", user_status
        return None, None
    except Exception:
        raise AISecurityUnavailable(
            "AI request controls are temporarily unavailable."
        ) from None


def acquire_ai_quota_slot(user_id, request_limit):
    connection = None
    cursor = None
    lock_name = f"reviewgrow:sync-ai:user:{int(user_id)}"
    try:
        connection = get_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT GET_LOCK(%s, 0) AS acquired", (lock_name,))
        acquired = _row_value(cursor.fetchone(), "acquired")
        if acquired != 1:
            raise AIRequestInProgress(
                "Another AI request is already in progress for this account."
            )

        cursor.execute(
            """
            SELECT COUNT(*) AS successful_requests
            FROM ai_usage_logs
            WHERE user_id=%s
              AND request_status='success'
              AND created_at >= DATE_FORMAT(UTC_TIMESTAMP(), '%%Y-%%m-01')
              AND created_at < DATE_ADD(
                  DATE_FORMAT(UTC_TIMESTAMP(), '%%Y-%%m-01'),
                  INTERVAL 1 MONTH
              )
            """,
            (user_id,),
        )
        used_requests = int(
            _row_value(cursor.fetchone(), "successful_requests") or 0
        )
        if used_requests >= request_limit:
            raise AIQuotaExceeded(
                "Monthly AI request quota has been reached."
            )
        return AIQuotaSlot(
            connection, cursor, lock_name, used_requests, request_limit
        )
    except (AIRequestInProgress, AIQuotaExceeded):
        if cursor is not None:
            try:
                cursor.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
            except Exception:
                pass
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()
        raise
    except Exception:
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()
        raise AISecurityUnavailable(
            "AI request controls are temporarily unavailable."
        ) from None


def _row_value(row, name):
    if isinstance(row, Mapping):
        normalized = {
            str(key).casefold(): value for key, value in row.items()
        }
        if name.casefold() in normalized:
            return normalized[name.casefold()]
    elif isinstance(row, (tuple, list)) and row:
        return row[0]
    raise AISecurityUnavailable(
        "AI request controls are temporarily unavailable."
    )
