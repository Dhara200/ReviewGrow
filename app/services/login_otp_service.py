"""Secure, single-use email OTP challenges for password login."""

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from app.config import Config
from app.services.database_service import get_connection
from app.services.email_queue_service import enqueue_email


OTP_EMAIL_SUBJECT = "Your ReviewGrow verification code"


class LoginOtpError(RuntimeError):
    code = "unavailable"


class OtpCooldownError(LoginOtpError):
    code = "cooldown"


class OtpRateLimitError(LoginOtpError):
    code = "rate_limited"


class OtpInvalidError(LoginOtpError):
    code = "invalid"


class OtpExpiredError(LoginOtpError):
    code = "expired"


class OtpLockedError(LoginOtpError):
    code = "locked"


@dataclass(frozen=True)
class CreatedChallenge:
    id: int
    masked_email: str


def generate_otp():
    return f"{secrets.randbelow(1_000_000):06d}"


def _otp_digest(challenge_id, nonce, otp):
    key = (Config.SECRET_KEY or "").encode("utf-8")
    if len(key) < 16:
        raise LoginOtpError("OTP signing configuration is unavailable.")
    payload = f"login:{challenge_id}:{nonce}:{otp}".encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _stored_hash(challenge_id, otp):
    nonce = secrets.token_hex(16)
    return f"hmac-sha256${nonce}${_otp_digest(challenge_id, nonce, otp)}"


def verify_otp_hash(challenge_id, stored_hash, otp):
    try:
        algorithm, nonce, expected = stored_hash.split("$", 2)
        if algorithm != "hmac-sha256":
            return False
        actual = _otp_digest(challenge_id, nonce, otp)
        return hmac.compare_digest(expected, actual)
    except (AttributeError, ValueError, LoginOtpError):
        return False


def _mask_email(email):
    local, separator, domain = (email or "").partition("@")
    if not separator:
        return "***"
    return f"{local[:1]}{'*' * max(3, len(local) - 1)}@{domain}"


def create_login_otp_challenge(user, requested_ip, user_agent, *, resend=False):
    """Invalidate older challenges, store an HMAC, commit, then enqueue email."""
    otp = generate_otp()
    connection = get_connection()
    cursor = connection.cursor(dictionary=True)
    challenge_id = None
    try:
        connection.start_transaction()
        cursor.execute(
            """SELECT user_id,requested_ip,last_sent_at
               FROM login_otp_challenges
               WHERE (user_id=%s OR requested_ip=%s)
                 AND created_at >= UTC_TIMESTAMP(6) - INTERVAL 15 MINUTE
               ORDER BY created_at DESC FOR UPDATE""", (user["id"], requested_ip),
        )
        recent_requests = cursor.fetchall()
        if len(recent_requests) >= Config.LOGIN_OTP_MAX_REQUESTS_PER_15_MINUTES:
            raise OtpRateLimitError("OTP request limit reached.")
        user_requests = [
            row for row in recent_requests if int(row["user_id"]) == int(user["id"])
        ]
        last_sent_at = user_requests[0].get("last_sent_at") if user_requests else None
        if resend and last_sent_at is not None:
            cursor.execute(
                "SELECT TIMESTAMPDIFF(SECOND,%s,UTC_TIMESTAMP(6)) AS elapsed",
                (last_sent_at,),
            )
            elapsed = int((cursor.fetchone() or {}).get("elapsed") or 0)
            if elapsed < Config.LOGIN_OTP_RESEND_COOLDOWN_SECONDS:
                raise OtpCooldownError("OTP resend cooldown is active.")

        cursor.execute(
            """UPDATE login_otp_challenges SET invalidated_at=UTC_TIMESTAMP(6),
               updated_at=UTC_TIMESTAMP(6)
               WHERE user_id=%s AND used_at IS NULL AND invalidated_at IS NULL""",
            (user["id"],),
        )
        cursor.execute(
            """UPDATE email_jobs SET status='cancelled',
               template_data=JSON_OBJECT('redacted',TRUE),
               last_error='Superseded by a newer login challenge',
               updated_at=UTC_TIMESTAMP(6)
               WHERE user_id=%s AND email_type='login_otp' AND status='pending'""",
            (user["id"],),
        )
        agent_hash = hashlib.sha256((user_agent or "").encode("utf-8")).digest()
        cursor.execute(
            """INSERT INTO login_otp_challenges
               (user_id,otp_hash,expires_at,attempt_count,max_attempts,requested_ip,
                user_agent_hash,resend_count,last_sent_at,created_at,updated_at)
               VALUES (%s,'pending',UTC_TIMESTAMP(6) + INTERVAL %s MINUTE,0,%s,%s,%s,
                       %s,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))""",
            (user["id"], Config.LOGIN_OTP_EXPIRY_MINUTES,
             Config.LOGIN_OTP_MAX_ATTEMPTS, requested_ip, agent_hash,
             1 if resend else 0),
        )
        challenge_id = cursor.lastrowid
        cursor.execute(
            "UPDATE login_otp_challenges SET otp_hash=%s WHERE id=%s",
            (_stored_hash(challenge_id, otp), challenge_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()

    try:
        enqueue_email(
            user["email"], "login_otp", "login_otp",
            {
                "subject": OTP_EMAIL_SUBJECT,
                "customer_name": user.get("name") or "",
                "otp_code": otp,
                "expiry_minutes": Config.LOGIN_OTP_EXPIRY_MINUTES,
                "support_email": Config.SES_REPLY_TO_EMAIL or "founder@reviewgrow.in",
                "challenge_id": challenge_id,
            },
            user_id=user["id"], priority=10,
            deduplication_key=f"login_otp:{challenge_id}",
        )
    except Exception:
        invalidate_challenge(challenge_id)
        raise
    finally:
        otp = None
    return CreatedChallenge(challenge_id, _mask_email(user["email"]))


def verify_login_otp(user_id, challenge_id, otp):
    connection = get_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        connection.start_transaction()
        cursor.execute(
            """SELECT c.*,u.name,u.email,u.role
               FROM login_otp_challenges c JOIN users u ON u.id=c.user_id
               WHERE c.id=%s FOR UPDATE""", (challenge_id,),
        )
        challenge = cursor.fetchone()
        if not challenge or int(challenge["user_id"]) != int(user_id):
            raise OtpInvalidError("OTP challenge is invalid.")
        if challenge.get("used_at") is not None or challenge.get("invalidated_at") is not None:
            raise OtpInvalidError("OTP challenge is no longer active.")
        cursor.execute("SELECT UTC_TIMESTAMP(6) >= %s AS expired", (challenge["expires_at"],))
        if (cursor.fetchone() or {}).get("expired"):
            cursor.execute(
                "UPDATE login_otp_challenges SET invalidated_at=UTC_TIMESTAMP(6),updated_at=UTC_TIMESTAMP(6) WHERE id=%s",
                (challenge_id,),
            )
            connection.commit()
            raise OtpExpiredError("OTP challenge expired.")
        if int(challenge["attempt_count"]) >= int(challenge["max_attempts"]):
            raise OtpLockedError("OTP challenge is locked.")
        if not verify_otp_hash(challenge_id, challenge["otp_hash"], otp):
            new_count = int(challenge["attempt_count"]) + 1
            locked = new_count >= int(challenge["max_attempts"])
            cursor.execute(
                """UPDATE login_otp_challenges SET attempt_count=%s,
                   invalidated_at=IF(%s,UTC_TIMESTAMP(6),invalidated_at),
                   updated_at=UTC_TIMESTAMP(6) WHERE id=%s""",
                (new_count, locked, challenge_id),
            )
            connection.commit()
            if locked:
                raise OtpLockedError("OTP challenge is locked.")
            raise OtpInvalidError("OTP is incorrect.")

        cursor.execute(
            """UPDATE login_otp_challenges SET used_at=UTC_TIMESTAMP(6),
               updated_at=UTC_TIMESTAMP(6)
               WHERE id=%s AND used_at IS NULL AND invalidated_at IS NULL""",
            (challenge_id,),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            raise OtpInvalidError("OTP challenge was already consumed.")
        cursor.execute(
            """UPDATE login_otp_challenges SET invalidated_at=UTC_TIMESTAMP(6),
               updated_at=UTC_TIMESTAMP(6)
               WHERE user_id=%s AND id<>%s AND used_at IS NULL AND invalidated_at IS NULL""",
            (user_id, challenge_id),
        )
        connection.commit()
        return {"id": challenge["user_id"], "name": challenge["name"],
                "email": challenge["email"], "role": challenge.get("role") or "owner"}
    except (OtpInvalidError, OtpExpiredError, OtpLockedError):
        if getattr(connection, "in_transaction", False):
            connection.rollback()
        raise
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def invalidate_challenge(challenge_id):
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """UPDATE login_otp_challenges SET invalidated_at=UTC_TIMESTAMP(6),
               updated_at=UTC_TIMESTAMP(6) WHERE id=%s AND used_at IS NULL""",
            (challenge_id,),
        )
        connection.commit()
    finally:
        cursor.close()
        connection.close()


def is_login_otp_challenge_active(challenge_id):
    connection = get_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """SELECT id FROM login_otp_challenges
               WHERE id=%s AND used_at IS NULL AND invalidated_at IS NULL
                 AND expires_at>UTC_TIMESTAMP(6) AND attempt_count<max_attempts""",
            (challenge_id,),
        )
        return cursor.fetchone() is not None
    finally:
        cursor.close()
        connection.close()
