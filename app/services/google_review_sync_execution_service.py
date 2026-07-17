from datetime import datetime, timedelta

from app.config import Config
from app.services.database_service import get_connection
from app.services.google_business_service import GoogleBusinessError, refresh_access_token
from app.services.review_sync_service import sync_google_reviews
from app.services.token_crypto_service import decrypt_token, encrypt_token


def run_google_review_sync(user_id, business_id):
    """Validate and execute one Google review sync without Flask session state."""
    connection = _load_owned_google_connection(user_id, business_id)
    connection = ensure_valid_google_connection_token(connection)
    return synchronize_google_reviews(connection)


def synchronize_google_reviews(connection):
    """Run the existing review mapping/upsert logic and record sync completion."""
    database = get_connection()
    cursor = database.cursor(dictionary=True)

    try:
        result = sync_google_reviews(cursor, connection)
        cursor.execute(
            """
            UPDATE google_business_connections
            SET last_sync_at=NOW()
            WHERE id=%s
              AND user_id=%s
              AND business_id=%s
            """,
            (
                connection["id"],
                connection["user_id"],
                connection["business_id"],
            ),
        )
        database.commit()
        return result
    except Exception:
        database.rollback()
        raise
    finally:
        cursor.close()
        database.close()


def ensure_valid_google_connection_token(connection):
    expiry = connection.get("token_expiry")
    if expiry and expiry > datetime.utcnow() + timedelta(minutes=5):
        return connection

    token_data = refresh_access_token(connection.get("refresh_token"))
    scope = token_data.get("scope") or Config.GOOGLE_SCOPES
    database = get_connection()
    cursor = database.cursor()

    try:
        cursor.execute(
            """
            UPDATE google_business_connections
            SET access_token=%s,
                token_expiry=%s,
                scope=COALESCE(%s, scope),
                scopes=COALESCE(%s, scopes)
            WHERE id=%s
              AND user_id=%s
              AND business_id=%s
            """,
            (
                encrypt_token(token_data["access_token"]),
                token_data["token_expiry"],
                scope,
                scope,
                connection["id"],
                connection["user_id"],
                connection["business_id"],
            ),
        )
        database.commit()
    except Exception:
        database.rollback()
        raise
    finally:
        cursor.close()
        database.close()

    connection["access_token"] = token_data["access_token"]
    connection["token_expiry"] = token_data["token_expiry"]
    return connection


def _load_owned_google_connection(user_id, business_id):
    database = get_connection()
    cursor = database.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT connection.*
            FROM users user_account
            JOIN businesses business
              ON business.user_id=user_account.id
             AND business.id=%s
            JOIN google_business_connections connection
              ON connection.business_id=business.id
             AND connection.user_id=user_account.id
            WHERE user_account.id=%s
              AND connection.is_connected=TRUE
            LIMIT 1
            """,
            (business_id, user_id),
        )
        connection = cursor.fetchone()
    finally:
        cursor.close()
        database.close()

    if not connection:
        raise GoogleBusinessError(
            "The user, business, or connected Google Business Profile account is unavailable."
        )
    if not connection.get("google_account_id") or not connection.get("google_location_id"):
        raise GoogleBusinessError("Google Business Profile location is not selected.")

    connection["access_token"] = decrypt_token(connection.get("access_token"))
    connection["refresh_token"] = decrypt_token(connection.get("refresh_token"))
    return connection

