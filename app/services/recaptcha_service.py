from dataclasses import dataclass

import requests
from flask import current_app


@dataclass(frozen=True)
class RecaptchaResult:
    success: bool
    reason: str = ""


def _is_non_production():
    environment = str(current_app.config.get("APP_ENV", "production")).lower()
    return (
        current_app.testing
        or environment in {"development", "dev", "local", "testing", "test"}
    )


def verify_recaptcha(token, expected_action, remote_ip=None):
    """Verify a reCAPTCHA v3 token without logging or persisting sensitive data."""
    enabled = bool(current_app.config.get("RECAPTCHA_ENABLED", True))

    if not enabled:
        if _is_non_production():
            return RecaptchaResult(True, "disabled_non_production")
        current_app.logger.error(
            "reCAPTCHA verification is disabled in a production environment"
        )
        return RecaptchaResult(False, "disabled_in_production")

    secret_key = str(current_app.config.get("RECAPTCHA_SECRET_KEY", "")).strip()
    site_key = str(current_app.config.get("RECAPTCHA_SITE_KEY", "")).strip()
    verify_url = str(current_app.config.get("RECAPTCHA_VERIFY_URL", "")).strip()
    if not site_key or not secret_key or not verify_url:
        current_app.logger.error(
            "reCAPTCHA configuration is incomplete for action=%s", expected_action
        )
        return RecaptchaResult(False, "configuration_error")

    submitted_token = (token or "").strip()
    if not submitted_token:
        current_app.logger.info(
            "reCAPTCHA verification failed for action=%s: missing token",
            expected_action
        )
        return RecaptchaResult(False, "missing_token")

    payload = {"secret": secret_key, "response": submitted_token}
    if remote_ip:
        payload["remoteip"] = remote_ip

    try:
        response = requests.post(
            verify_url,
            data=payload,
            timeout=float(current_app.config.get("RECAPTCHA_TIMEOUT_SECONDS", 5))
        )
        response.raise_for_status()
        verification = response.json()
    except (requests.RequestException, ValueError, TypeError):
        current_app.logger.warning(
            "reCAPTCHA provider request failed for action=%s", expected_action
        )
        return RecaptchaResult(False, "provider_error")

    if not isinstance(verification, dict):
        current_app.logger.warning(
            "reCAPTCHA provider returned malformed data for action=%s", expected_action
        )
        return RecaptchaResult(False, "malformed_response")

    if verification.get("success") is not True:
        current_app.logger.info(
            "reCAPTCHA verification failed for action=%s", expected_action
        )
        return RecaptchaResult(False, "invalid_token")

    if verification.get("action") != expected_action:
        current_app.logger.info(
            "reCAPTCHA action mismatch for expected action=%s", expected_action
        )
        return RecaptchaResult(False, "action_mismatch")

    try:
        score = float(verification["score"])
        threshold = float(current_app.config.get("RECAPTCHA_SCORE_THRESHOLD", 0.5))
    except (KeyError, TypeError, ValueError):
        current_app.logger.warning(
            "reCAPTCHA provider returned malformed data for action=%s", expected_action
        )
        return RecaptchaResult(False, "malformed_response")

    if score < threshold:
        current_app.logger.info(
            "reCAPTCHA score was below threshold for action=%s", expected_action
        )
        return RecaptchaResult(False, "low_score")

    return RecaptchaResult(True)
