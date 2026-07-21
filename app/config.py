import os
from dotenv import load_dotenv

load_dotenv()


def _get_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_non_negative_float(name, default):
    try:
        value = float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)
    return value if value >= 0 else float(default)


def _get_positive_int(name, default, minimum=1):
    try:
        value = int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)
    return value if value >= minimum else int(default)


class Config:
    APP_ENV = os.getenv("APP_ENV", "production").strip().lower()
    PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "https://reviewgrow.in").rstrip("/")
    DEBUG = _get_bool("APP_DEBUG", False)
    TESTING = False
    DB_HOST = os.getenv("DB_HOST")
    DB_PORT = int(os.getenv("DB_PORT", 3306))
    DB_NAME = os.getenv("DB_NAME")
    DB_USER = os.getenv("DB_USER")
    DB_PASSWORD = os.getenv("DB_PASSWORD")
    TRUSTED_PROXY_IPS = tuple(
        value.strip()
        for value in os.getenv("TRUSTED_PROXY_IPS", "127.0.0.1,::1").split(",")
        if value.strip()
    )

    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

    GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
    GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")
    GOOGLE_SCOPES = os.getenv(
        "GOOGLE_SCOPES",
        "openid email profile https://www.googleapis.com/auth/business.manage"
    )

    SUBSCRIPTION_PRICE = float(os.getenv("SUBSCRIPTION_PRICE", 1999))
    ORIGINAL_SUBSCRIPTION_PRICE = float(os.getenv("ORIGINAL_SUBSCRIPTION_PRICE", 2999))
    RAZORPAY_KEY_ID = (os.getenv("RAZORPAY_KEY_ID") or "").strip()
    RAZORPAY_KEY_SECRET = (os.getenv("RAZORPAY_KEY_SECRET") or "").strip()
    RAZORPAY_WEBHOOK_SECRET = (os.getenv("RAZORPAY_WEBHOOK_SECRET") or "").strip()
    CONTACT_PHONE = (os.getenv("CONTACT_PHONE") or "8778358580").strip()
    WHATSAPP_NUMBER = (os.getenv("WHATSAPP_NUMBER") or "8778358580").strip()

    MAX_LOGIN_ATTEMPTS = int(os.getenv("MAX_LOGIN_ATTEMPTS", 5))
    LOGIN_LOCK_MINUTES = int(os.getenv("LOGIN_LOCK_MINUTES", 15))
    LOGIN_WINDOW_MINUTES = int(os.getenv("LOGIN_WINDOW_MINUTES", 15))

    SECRET_KEY = os.getenv("SECRET_KEY")

    RECAPTCHA_ENABLED = _get_bool("RECAPTCHA_ENABLED", True)
    RECAPTCHA_SITE_KEY = (os.getenv("RECAPTCHA_SITE_KEY") or "").strip()
    RECAPTCHA_SECRET_KEY = (os.getenv("RECAPTCHA_SECRET_KEY") or "").strip()
    RECAPTCHA_SCORE_THRESHOLD = float(os.getenv("RECAPTCHA_SCORE_THRESHOLD", 0.5))
    RECAPTCHA_VERIFY_URL = os.getenv(
        "RECAPTCHA_VERIFY_URL",
        "https://www.google.com/recaptcha/api/siteverify"
    ).strip()
    RECAPTCHA_TIMEOUT_SECONDS = float(os.getenv("RECAPTCHA_TIMEOUT_SECONDS", 5))

    AI_PROVIDER = os.getenv("AI_PROVIDER", "gemini")
    AI_MODEL_NAME = os.getenv("AI_MODEL_NAME", "gemini-2.5-flash")
    AI_BATCH_SIZE = int(os.getenv("AI_BATCH_SIZE", 25))
    AI_WORKER_POLL_SECONDS = int(os.getenv("AI_WORKER_POLL_SECONDS", 5))
    WORKER_ERROR_BACKOFF_SECONDS = _get_non_negative_float(
        "WORKER_ERROR_BACKOFF_SECONDS", 5
    )
    GOOGLE_REVIEW_SYNC_MAX_RETRIES = int(os.getenv("GOOGLE_REVIEW_SYNC_MAX_RETRIES", 3))
    GOOGLE_REVIEW_SYNC_BACKOFF_BASE_SECONDS = float(
        os.getenv("GOOGLE_REVIEW_SYNC_BACKOFF_BASE_SECONDS", 2)
    )
    GOOGLE_REVIEW_SYNC_BACKOFF_JITTER_SECONDS = float(
        os.getenv("GOOGLE_REVIEW_SYNC_BACKOFF_JITTER_SECONDS", 0.5)
    )
    GOOGLE_REVIEW_SYNC_STALE_TIMEOUT_MINUTES = int(
        os.getenv("GOOGLE_REVIEW_SYNC_STALE_TIMEOUT_MINUTES", 30)
    )
    GOOGLE_REVIEW_SYNC_LEASE_SECONDS = _get_positive_int(
        "GOOGLE_REVIEW_SYNC_LEASE_SECONDS", 120, minimum=2
    )
    GOOGLE_REVIEW_SYNC_HEARTBEAT_SECONDS = _get_positive_int(
        "GOOGLE_REVIEW_SYNC_HEARTBEAT_SECONDS", 30
    )
    if GOOGLE_REVIEW_SYNC_HEARTBEAT_SECONDS >= GOOGLE_REVIEW_SYNC_LEASE_SECONDS:
        GOOGLE_REVIEW_SYNC_HEARTBEAT_SECONDS = max(
            1, GOOGLE_REVIEW_SYNC_LEASE_SECONDS // 4
        )
    MAX_REVIEWS_PER_MONTH = int(os.getenv("MAX_REVIEWS_PER_MONTH", 0))
    MAX_AI_REQUESTS_PER_MONTH = int(os.getenv("MAX_AI_REQUESTS_PER_MONTH", 0))
    MAX_TOKENS_PER_MONTH = int(os.getenv("MAX_TOKENS_PER_MONTH", 0))
    GEMINI_FLASH_INPUT_COST_PER_1M = float(os.getenv("GEMINI_FLASH_INPUT_COST_PER_1M", 0.30))
    GEMINI_FLASH_OUTPUT_COST_PER_1M = float(os.getenv("GEMINI_FLASH_OUTPUT_COST_PER_1M", 2.50))
