import os
from dotenv import load_dotenv

load_dotenv()


def _get_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

class Config:
    APP_ENV = os.getenv("APP_ENV", "production").strip().lower()
    DEBUG = _get_bool("APP_DEBUG", False)
    TESTING = False
    DB_HOST = os.getenv("DB_HOST")
    DB_PORT = int(os.getenv("DB_PORT", 3306))
    DB_NAME = os.getenv("DB_NAME")
    DB_USER = os.getenv("DB_USER")
    DB_PASSWORD = os.getenv("DB_PASSWORD")

    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

    GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
    GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")
    GOOGLE_SCOPES = os.getenv(
        "GOOGLE_SCOPES",
        "openid email profile https://www.googleapis.com/auth/business.manage"
    )

    UPI_ID = os.getenv("UPI_ID", "dharaprasath52@okhdfcbank")
    SUBSCRIPTION_PRICE = float(os.getenv("SUBSCRIPTION_PRICE", 1999))
    ORIGINAL_SUBSCRIPTION_PRICE = float(os.getenv("ORIGINAL_SUBSCRIPTION_PRICE", 2999))

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
    MAX_REVIEWS_PER_MONTH = int(os.getenv("MAX_REVIEWS_PER_MONTH", 0))
    MAX_AI_REQUESTS_PER_MONTH = int(os.getenv("MAX_AI_REQUESTS_PER_MONTH", 0))
    MAX_TOKENS_PER_MONTH = int(os.getenv("MAX_TOKENS_PER_MONTH", 0))
    GEMINI_FLASH_INPUT_COST_PER_1M = float(os.getenv("GEMINI_FLASH_INPUT_COST_PER_1M", 0.30))
    GEMINI_FLASH_OUTPUT_COST_PER_1M = float(os.getenv("GEMINI_FLASH_OUTPUT_COST_PER_1M", 2.50))
