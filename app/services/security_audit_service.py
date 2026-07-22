"""Structured, privacy-conscious application security audit events.

Events are emitted to the existing application logger. Retention is therefore
controlled by the current Docker/logging platform, not by this service.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
from datetime import datetime, timezone

from app.services.login_security_service import normalize_login_email


EVENT_CATALOG = {
    "login_success": ("info", "success"),
    "login_invalid_credentials": ("warning", "failure"),
    "login_rate_limited": ("warning", "blocked"),
    "login_csrf_rejected": ("warning", "rejected"),
    "login_recaptcha_rejected": ("warning", "rejected"),
    "login_input_rejected": ("warning", "rejected"),
    "login_limiter_unavailable": ("error", "error"),
    "login_backend_unavailable": ("error", "error"),
    "login_internal_error": ("error", "error"),
}

ALLOWED_EVENT_FIELDS = frozenset({
    "limiter_scope", "retry_after_seconds", "http_status", "failure_category",
})
PROHIBITED_FIELDS = frozenset({
    "email", "raw_email", "password", "password_hash", "dummy_hash", "csrf",
    "csrf_token", "recaptcha", "recaptcha_token", "token", "cookie", "session",
    "session_id", "secret", "secret_key", "database_url", "db_password", "raw_ip",
    "x_forwarded_for", "limiter_key", "key_hash", "exception_message", "sql",
})
ALLOWED_LIMITER_SCOPES = frozenset({"ip", "account", "ip_account", "multiple"})


class SecurityAuditService:
    PREFIX = "security_audit "

    def __init__(self, logger, *, enabled, hmac_key):
        self._logger = logger
        self._enabled = bool(enabled)
        self._hmac_key = hmac_key.encode("utf-8") if hmac_key else b""

    def account_key(self, email, *, valid=True):
        normalized = normalize_login_email(email)
        if not normalized:
            return "missing"
        if not valid:
            return "invalid"
        return self._pseudonym("account", normalized)

    def client_key(self, client_ip):
        try:
            normalized = str(ipaddress.ip_address(client_ip or ""))
        except ValueError:
            return "unknown"
        return self._pseudonym("client", normalized)

    def emit(
        self,
        event_name,
        *,
        email=None,
        account_valid=True,
        client_ip=None,
        **fields,
    ):
        if not self._enabled:
            return None
        if event_name not in EVENT_CATALOG:
            raise ValueError(f"Unsupported security audit event: {event_name!r}.")
        supplied_fields = set(fields)
        prohibited = supplied_fields & PROHIBITED_FIELDS
        if prohibited:
            raise ValueError("Prohibited security audit field supplied.")
        unknown = supplied_fields - ALLOWED_EVENT_FIELDS
        if unknown:
            raise ValueError("Unknown security audit field supplied.")
        self._validate_fields(fields)

        level, outcome = EVENT_CATALOG[event_name]
        event = {
            "account_key": self.account_key(email, valid=account_valid),
            "client_ip_key": self.client_key(client_ip),
            "event_name": event_name,
            "event_version": 1,
            "outcome": outcome,
            "timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        event.update(fields)
        rendered = self.PREFIX + json.dumps(
            event, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        getattr(self._logger, level)(rendered)
        return event

    def _pseudonym(self, namespace, value):
        payload = f"{namespace}\0{value}".encode("utf-8")
        return hmac.new(self._hmac_key, payload, hashlib.sha256).hexdigest()

    @staticmethod
    def _validate_fields(fields):
        status = fields.get("http_status")
        if status is not None and (
            isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599
        ):
            raise ValueError("http_status must be a valid integer HTTP status.")
        retry = fields.get("retry_after_seconds")
        if retry is not None and (
            isinstance(retry, bool) or not isinstance(retry, int) or retry < 1
        ):
            raise ValueError("retry_after_seconds must be a positive integer.")
        scope = fields.get("limiter_scope")
        if scope is not None and scope not in ALLOWED_LIMITER_SCOPES:
            raise ValueError("Unsupported limiter_scope.")
        category = fields.get("failure_category")
        if category is not None and (
            not isinstance(category, str)
            or not category
            or len(category) > 64
            or not category.replace("_", "").isalnum()
        ):
            raise ValueError("failure_category must be a controlled identifier.")


def validate_security_audit_config(app):
    enabled = app.config.get("SECURITY_AUDIT_ENABLED")
    if not isinstance(enabled, bool):
        raise RuntimeError("SECURITY_AUDIT_ENABLED must be a boolean.")
    if not enabled:
        return
    key = app.config.get("SECURITY_AUDIT_HMAC_KEY")
    unsafe_placeholder = isinstance(key, str) and (
        key.lower().startswith("replace_")
        or "change_me" in key.lower()
        or "example" in key.lower()
    )
    if (
        not isinstance(key, str)
        or len(key) < 32
        or len(set(key)) < 8
        or unsafe_placeholder
    ):
        raise RuntimeError(
            "SECURITY_AUDIT_HMAC_KEY must be a strong value of at least 32 characters."
        )


def recaptcha_failure_category(reason):
    return {
        "missing_token": "token_missing",
        "provider_error": "provider_error",
        "malformed_response": "provider_error",
        "low_score": "score_rejected",
        "action_mismatch": "action_mismatch",
        "invalid_token": "token_invalid",
        "configuration_error": "configuration_error",
        "disabled_in_production": "configuration_error",
    }.get(reason, "verification_rejected")
