"""Reusable rate-limiter primitives backed by MySQL.

This module deliberately has no Flask or authentication dependencies.  Callers use
``LimiterService`` and can therefore receive a different backend in the future.
"""

from __future__ import annotations

import hashlib
import ipaddress
from dataclasses import dataclass
from typing import Protocol

from app.services.database_service import get_connection


SUPPORTED_SCOPES = frozenset({"ip", "account", "ip_account"})


@dataclass(frozen=True)
class LimitStatus:
    blocked: bool
    attempt_count: int
    retry_after_seconds: int


class LimiterBackend(Protocol):
    def check_limit(self, scope: str, key_hash: bytes) -> LimitStatus: ...

    def record_failure(
        self,
        scope: str,
        key_hash: bytes,
        *,
        threshold: int,
        window_seconds: int,
        block_seconds: int,
    ) -> LimitStatus: ...

    def reset(self, scope: str, key_hash: bytes) -> bool: ...

    def cleanup(self, *, older_than_seconds: int, limit: int) -> int: ...


def normalize_key(scope: str, key) -> str:
    """Return a stable canonical key without retaining the caller's raw value."""
    _validate_scope(scope)
    if scope == "ip":
        return ipaddress.ip_address(_required_text(key)).compressed.lower()
    if scope == "account":
        return _required_text(key).casefold()

    if not isinstance(key, (tuple, list)) or len(key) != 2:
        raise ValueError("ip_account keys must be a two-item (ip, account) sequence.")
    ip_value = normalize_key("ip", key[0])
    account_value = normalize_key("account", key[1])
    # Length-prefixing prevents ambiguous composite keys.
    return f"{len(ip_value)}:{ip_value}{len(account_value)}:{account_value}"


def hash_key(scope: str, key) -> bytes:
    canonical = normalize_key(scope, key)
    return hashlib.sha256(canonical.encode("utf-8")).digest()


class LimiterService:
    """Backend-independent public API for rate-limiting state."""

    def __init__(self, backend: LimiterBackend | None = None):
        self._backend = backend or MySQLLimiterBackend()

    def check_limit(self, scope: str, key) -> LimitStatus:
        return self._backend.check_limit(scope, hash_key(scope, key))

    def record_failure(
        self,
        scope: str,
        key,
        *,
        threshold: int,
        window_seconds: int,
        block_seconds: int,
    ) -> LimitStatus:
        _validate_positive("threshold", threshold)
        _validate_positive("window_seconds", window_seconds)
        _validate_positive("block_seconds", block_seconds)
        return self._backend.record_failure(
            scope,
            hash_key(scope, key),
            threshold=threshold,
            window_seconds=window_seconds,
            block_seconds=block_seconds,
        )

    def reset(self, scope: str, key) -> bool:
        return self._backend.reset(scope, hash_key(scope, key))

    def cleanup(self, *, older_than_seconds: int, limit: int = 1000) -> int:
        _validate_positive("older_than_seconds", older_than_seconds)
        _validate_positive("limit", limit)
        return self._backend.cleanup(
            older_than_seconds=older_than_seconds,
            limit=limit,
        )


class MySQLLimiterBackend:
    """Transaction-protected MySQL implementation of ``LimiterBackend``."""

    def __init__(self, connection_factory=get_connection):
        self._connection_factory = connection_factory

    def check_limit(self, scope: str, key_hash: bytes) -> LimitStatus:
        _validate_backend_key(scope, key_hash)
        connection = self._connection_factory()
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                UPDATE rate_limit_counters
                SET attempt_count=0,
                    blocked_until=NULL,
                    window_started_at=UTC_TIMESTAMP(6),
                    updated_at=UTC_TIMESTAMP(6)
                WHERE scope=%s AND key_hash=%s
                  AND blocked_until IS NOT NULL
                  AND blocked_until <= UTC_TIMESTAMP(6)
                """,
                (scope, key_hash),
            )
            cursor.execute(
                """
                SELECT
                    (blocked_until > UTC_TIMESTAMP(6)) AS blocked,
                    attempt_count,
                    GREATEST(0, COALESCE(
                        TIMESTAMPDIFF(SECOND, UTC_TIMESTAMP(6), blocked_until), 0
                    )) AS retry_after_seconds
                FROM rate_limit_counters
                WHERE scope=%s AND key_hash=%s
                """,
                (scope, key_hash),
            )
            row = cursor.fetchone()
            connection.commit()
            return _status(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def record_failure(
        self,
        scope: str,
        key_hash: bytes,
        *,
        threshold: int,
        window_seconds: int,
        block_seconds: int,
    ) -> LimitStatus:
        _validate_backend_key(scope, key_hash)
        for name, value in (
            ("threshold", threshold),
            ("window_seconds", window_seconds),
            ("block_seconds", block_seconds),
        ):
            _validate_positive(name, value)

        connection = self._connection_factory()
        cursor = connection.cursor(dictionary=True)
        try:
            # INSERT IGNORE is duplicate-safe; FOR UPDATE serializes all subsequent
            # state transitions for this limiter key until commit.
            cursor.execute(
                """
                INSERT IGNORE INTO rate_limit_counters
                    (scope, key_hash, window_started_at, attempt_count,
                     created_at, updated_at)
                VALUES (%s, %s, UTC_TIMESTAMP(6), 0,
                        UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))
                """,
                (scope, key_hash),
            )
            cursor.execute(
                """
                SELECT id FROM rate_limit_counters
                WHERE scope=%s AND key_hash=%s
                FOR UPDATE
                """,
                (scope, key_hash),
            )
            if cursor.fetchone() is None:
                raise RuntimeError("Limiter row could not be created.")

            # MySQL evaluates single-table assignments left-to-right.  Compute the
            # counter from the old window/block, then the window from the old block,
            # and finally the block decision from the new counter.
            cursor.execute(
                """
                UPDATE rate_limit_counters
                SET attempt_count=CASE
                        WHEN blocked_until > UTC_TIMESTAMP(6) THEN attempt_count + 1
                        WHEN blocked_until IS NOT NULL
                             OR window_started_at <= DATE_SUB(
                                 UTC_TIMESTAMP(6), INTERVAL %s SECOND
                             ) THEN 1
                        ELSE attempt_count + 1
                    END,
                    window_started_at=CASE
                        WHEN (
                            blocked_until IS NOT NULL
                            AND blocked_until <= UTC_TIMESTAMP(6)
                        ) OR window_started_at <= DATE_SUB(
                            UTC_TIMESTAMP(6), INTERVAL %s SECOND
                        ) THEN UTC_TIMESTAMP(6)
                        ELSE window_started_at
                    END,
                    blocked_until=CASE
                        WHEN blocked_until > UTC_TIMESTAMP(6) THEN blocked_until
                        WHEN attempt_count >= %s THEN DATE_ADD(
                            UTC_TIMESTAMP(6), INTERVAL %s SECOND
                        )
                        ELSE NULL
                    END,
                    updated_at=UTC_TIMESTAMP(6)
                WHERE scope=%s AND key_hash=%s
                """,
                (
                    window_seconds, window_seconds, threshold, block_seconds,
                    scope, key_hash,
                ),
            )
            cursor.execute(
                """
                SELECT
                    (blocked_until > UTC_TIMESTAMP(6)) AS blocked,
                    attempt_count,
                    GREATEST(0, COALESCE(
                        TIMESTAMPDIFF(SECOND, UTC_TIMESTAMP(6), blocked_until), 0
                    )) AS retry_after_seconds
                FROM rate_limit_counters
                WHERE scope=%s AND key_hash=%s
                """,
                (scope, key_hash),
            )
            status = _status(cursor.fetchone())
            connection.commit()
            return status
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def reset(self, scope: str, key_hash: bytes) -> bool:
        _validate_backend_key(scope, key_hash)
        connection = self._connection_factory()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "DELETE FROM rate_limit_counters WHERE scope=%s AND key_hash=%s",
                (scope, key_hash),
            )
            removed = cursor.rowcount == 1
            connection.commit()
            return removed
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def cleanup(self, *, older_than_seconds: int, limit: int) -> int:
        _validate_positive("older_than_seconds", older_than_seconds)
        _validate_positive("limit", limit)
        connection = self._connection_factory()
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                DELETE FROM rate_limit_counters
                WHERE updated_at < DATE_SUB(UTC_TIMESTAMP(6), INTERVAL %s SECOND)
                  AND (blocked_until IS NULL OR blocked_until <= UTC_TIMESTAMP(6))
                ORDER BY updated_at ASC
                LIMIT %s
                """,
                (older_than_seconds, limit),
            )
            removed = cursor.rowcount
            connection.commit()
            return removed
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()


def _status(row) -> LimitStatus:
    if not row:
        return LimitStatus(False, 0, 0)
    return LimitStatus(
        blocked=bool(row["blocked"]),
        attempt_count=int(row["attempt_count"]),
        retry_after_seconds=int(row["retry_after_seconds"]),
    )


def _required_text(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Limiter keys must be non-empty strings.")
    return value.strip()


def _validate_scope(scope: str) -> None:
    if scope not in SUPPORTED_SCOPES:
        raise ValueError(f"Unsupported limiter scope: {scope!r}.")


def _validate_backend_key(scope: str, key_hash: bytes) -> None:
    _validate_scope(scope)
    if not isinstance(key_hash, bytes) or len(key_hash) != 32:
        raise ValueError("Limiter key hashes must be 32-byte SHA-256 digests.")


def _validate_positive(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
