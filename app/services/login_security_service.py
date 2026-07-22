"""Input and configuration validation for the login authentication boundary."""

import unicodedata

from werkzeug.security import check_password_hash


LOGIN_INPUT_ERROR_MESSAGE = "Invalid email or password."
LOGIN_APPLICATION_ERROR_MESSAGE = (
    "We could not complete your login right now. Please try again."
)
LOGIN_CSRF_ERROR_MESSAGE = "Security validation failed. Refresh the page and try again."


def normalize_login_email(value):
    return (value or "").strip().lower()


def validate_login_input(email, password):
    if not email or len(email) > 254 or _has_unsafe_control(email):
        return False
    if password is None or password == "" or len(password) > 128:
        return False
    if _has_unsafe_control(password):
        return False
    return True


def validate_login_dummy_hash(app):
    dummy_hash = app.config.get("LOGIN_DUMMY_PASSWORD_HASH")
    if not isinstance(dummy_hash, str) or not dummy_hash:
        raise RuntimeError("LOGIN_DUMMY_PASSWORD_HASH must be a valid password hash.")
    # Werkzeug returns False for a structurally valid hash and an unrelated value.
    # Known method prefixes and delimiters reject plaintext and malformed overrides.
    if not dummy_hash.startswith(("scrypt:", "pbkdf2:")) or dummy_hash.count("$") != 2:
        raise RuntimeError("LOGIN_DUMMY_PASSWORD_HASH must be a valid password hash.")
    try:
        check_password_hash(dummy_hash, "login-dummy-hash-validation-probe")
    except Exception as error:
        raise RuntimeError(
            "LOGIN_DUMMY_PASSWORD_HASH must be a valid password hash."
        ) from error


def _has_unsafe_control(value):
    return any(unicodedata.category(character) == "Cc" for character in value)
