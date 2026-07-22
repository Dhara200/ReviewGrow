from datetime import datetime, timedelta
import ipaddress
import re
import unicodedata

import mysql.connector
from flask import (
    Blueprint,
    current_app,
    jsonify,
    request,
    render_template,
    redirect,
    session
)
from werkzeug.security import (
    generate_password_hash,
    check_password_hash
)
from app.config import Config
from app.services.database_service import get_connection
from app.services.subscription_service import create_expired_subscription, has_active_subscription
from app.services.recaptcha_service import verify_recaptcha
from app.services.login_limiter_service import (
    LOGIN_LIMITER_UNAVAILABLE_MESSAGE,
    THROTTLED_LOGIN_MESSAGE,
    LoginLimiter,
    LoginLimiterPolicy,
    longest_retry_after,
)
from app.services.csrf_service import validate_login_csrf
from app.services.login_security_service import (
    LOGIN_APPLICATION_ERROR_MESSAGE,
    LOGIN_CSRF_ERROR_MESSAGE,
    normalize_login_email,
    login_input_failure,
    validate_login_input,
)
from app.services.security_audit_service import (
    SecurityAuditService,
    recaptcha_failure_category,
)

auth_bp = Blueprint("auth", __name__)
INVALID_LOGIN_MESSAGE = "Invalid email or password."
LOCKED_LOGIN_MESSAGE = "Too many failed login attempts. Please try again after 15 minutes."
RECAPTCHA_ERROR_MESSAGE = "Security verification failed. Please try again."
EMAIL_LOCAL_PATTERN = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+$")


def _registration_validation_error(name, email, password, confirm_password):
    if not name:
        return "Full name is required."
    if len(name) < 2:
        return "Full name must be at least 2 characters."
    if len(name) > 100:
        return "Full name must be no more than 100 characters."
    if any(unicodedata.category(character) == "Cc" for character in name):
        return "Full name cannot contain control characters."
    if not all(
        character.isalpha()
        or unicodedata.category(character).startswith("M")
        or unicodedata.category(character) in {"Zs", "Pd"}
        or character in {"'", "’", "."}
        for character in name
    ):
        return "Full name may contain letters, spaces, apostrophes, hyphens, and periods only."

    if not email:
        return "Email address is required."
    if len(email) > 254:
        return "Email address must be no more than 254 characters."
    if any(unicodedata.category(character) == "Cc" for character in email):
        return "Enter a valid email address."
    if email.count("@") != 1:
        return "Enter a valid email address."
    local_part, domain = email.rsplit("@", 1)
    if (
        not local_part
        or len(local_part) > 64
        or not EMAIL_LOCAL_PATTERN.fullmatch(local_part)
        or local_part.startswith(".")
        or local_part.endswith(".")
        or ".." in local_part
        or not _valid_email_domain(domain)
    ):
        return "Enter a valid email address."

    if not password:
        return "Password is required."
    if len(password) < 12:
        return "Password must be at least 12 characters."
    if len(password) > 128:
        return "Password must be no more than 128 characters."
    if not confirm_password:
        return "Please confirm your password."
    if password != confirm_password:
        return "Passwords do not match."
    return None


def _valid_email_domain(domain):
    if not domain or "." not in domain or domain.startswith(".") or domain.endswith("."):
        return False
    try:
        ascii_domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    if len(ascii_domain) > 253:
        return False
    labels = ascii_domain.split(".")
    return all(
        label
        and len(label) <= 63
        and not label.startswith("-")
        and not label.endswith("-")
        and all(character.isalnum() or character == "-" for character in label)
        for label in labels
    )


def _create_registered_user(name, email, password_hash):
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        conn.start_transaction()
        cursor.execute(
            """
            INSERT INTO users
            (name, email, password_hash)
            VALUES (%s, %s, %s)
            """,
            (name, email, password_hash)
        )
        user_id = cursor.lastrowid
        create_expired_subscription(
            user_id, connection=conn, cursor=cursor
        )
        conn.commit()
        return user_id
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def get_client_ip():
    try:
        return str(ipaddress.ip_address(request.remote_addr or ""))[:100]
    except ValueError:
        return "unknown"


def _login_error_response(
    message, email="", status_code=401, locked_until=None, retry_after_seconds=None
):
    lock_seconds = retry_after_seconds

    if lock_seconds is None and locked_until:
        lock_seconds = max(
            0,
            int((locked_until - datetime.utcnow()).total_seconds())
        )

    return render_template(
        "login.html",
        login_error=message,
        email=email,
        login_lock_seconds=lock_seconds
    ), status_code


def _throttled_login_response(
    retry_after_seconds, audit, email, ip_address, *, account_valid, limiter_scope
):
    retry_after_seconds = max(1, int(retry_after_seconds))
    audit.emit(
        "login_rate_limited",
        email=email,
        account_valid=account_valid,
        client_ip=ip_address,
        limiter_scope=limiter_scope,
        retry_after_seconds=retry_after_seconds,
        http_status=429,
    )
    response = current_app.make_response(
        _login_error_response(
            THROTTLED_LOGIN_MESSAGE,
            status_code=429,
            retry_after_seconds=retry_after_seconds,
        )
    )
    response.headers["Retry-After"] = str(retry_after_seconds)
    return response


def _limiter_unavailable_response(
    audit, email, ip_address, *, account_valid, failure_category
):
    audit.emit(
        "login_limiter_unavailable",
        email=email,
        account_valid=account_valid,
        client_ip=ip_address,
        failure_category=failure_category,
        http_status=503,
    )
    return _login_error_response(
        LOGIN_LIMITER_UNAVAILABLE_MESSAGE,
        status_code=503,
    )


def _login_application_error_response(
    audit, email, ip_address, *, account_valid, unavailable=False,
    failure_category="unexpected_failure",
):
    audit.emit(
        "login_backend_unavailable" if unavailable else "login_internal_error",
        email=email,
        account_valid=account_valid,
        client_ip=ip_address,
        failure_category=failure_category,
        http_status=503 if unavailable else 500,
    )
    if unavailable:
        return _login_error_response(
            LOGIN_LIMITER_UNAVAILABLE_MESSAGE,
            status_code=503,
        )
    return _login_error_response(
        LOGIN_APPLICATION_ERROR_MESSAGE,
        status_code=500,
    )


def _get_login_limiter():
    return LoginLimiter(LoginLimiterPolicy.from_config(current_app.config))


def _get_security_audit():
    return SecurityAuditService(
        current_app.logger,
        enabled=current_app.config.get("SECURITY_AUDIT_ENABLED", False),
        hmac_key=current_app.config.get("SECURITY_AUDIT_HMAC_KEY", ""),
    )


def _find_login_user(email):
    connection = None
    cursor = None
    try:
        connection = get_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT * FROM users WHERE email=%s",
            (email,),
        )
        return cursor.fetchone()
    except Exception:
        if connection is not None:
            try:
                connection.rollback()
            except Exception:
                pass
        raise
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


def _find_user_business(user_id):
    connection = None
    cursor = None
    try:
        connection = get_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT id FROM businesses WHERE user_id=%s LIMIT 1",
            (user_id,),
        )
        return cursor.fetchone()
    except Exception:
        if connection is not None:
            try:
                connection.rollback()
            except Exception:
                pass
        raise
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


def is_login_locked(email, ip_address):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT locked_until
        FROM login_attempts
        WHERE email=%s
        AND ip_address=%s
        AND locked_until IS NOT NULL
        AND locked_until > UTC_TIMESTAMP()
        LIMIT 1
        """,
        (email, ip_address)
    )
    attempt = cursor.fetchone()
    cursor.close()
    conn.close()

    return attempt["locked_until"] if attempt else None


def record_failed_login(email, ip_address):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute(
        """
        SELECT failed_attempts, last_failed_at
        FROM login_attempts
        WHERE email=%s
        AND ip_address=%s
        LIMIT 1
        """,
        (email, ip_address)
    )
    attempt = cursor.fetchone()

    now = datetime.utcnow()
    window_start = now - timedelta(minutes=Config.LOGIN_WINDOW_MINUTES)

    if attempt and attempt["last_failed_at"] and attempt["last_failed_at"] >= window_start:
        failed_attempts = int(attempt["failed_attempts"] or 0) + 1
    else:
        failed_attempts = 1

    locked_until = None

    # Lock the email + IP pair as soon as the configured attempt limit is reached.
    if failed_attempts >= Config.MAX_LOGIN_ATTEMPTS:
        locked_until = now + timedelta(minutes=Config.LOGIN_LOCK_MINUTES)
    if attempt:
        cursor.execute(
            """
            UPDATE login_attempts
            SET failed_attempts=%s,
                locked_until=%s,
                last_failed_at=%s
            WHERE email=%s
            AND ip_address=%s
            """,
            (
                failed_attempts,
                locked_until,
                now,
                email,
                ip_address
            )
        )
    else:
        cursor.execute(
            """
            INSERT INTO login_attempts
            (
                email,
                ip_address,
                failed_attempts,
                locked_until,
                last_failed_at
            )
            VALUES
            (%s,%s,%s,%s,%s)
            """,
            (
                email,
                ip_address,
                failed_attempts,
                locked_until,
                now
            )
        )

    conn.commit()
    cursor.close()
    conn.close()

    return locked_until


def reset_failed_login(email, ip_address):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE login_attempts
        SET failed_attempts=0,
            locked_until=NULL,
            last_failed_at=NULL
        WHERE email=%s
        AND ip_address=%s
        """,
        (email, ip_address)
    )
    conn.commit()
    cursor.close()
    conn.close()

#REGISTER PAGE ROUTE AUTHENTICATION

@auth_bp.route("/register-page", methods=["GET"])
def register_page():
    return render_template(
        "register.html"
    )   
@auth_bp.route("/register-page", methods=["POST"])
def register_form():
    
  try:
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    recaptcha_result = verify_recaptcha(
        request.form.get("recaptcha_token"),
        "register",
        get_client_ip()
    )
    if not recaptcha_result.success:
        return render_template(
            "register.html",
            register_error=RECAPTCHA_ERROR_MESSAGE,
            name=name,
            email=email
        ), 400

    password = request.form.get("password") or ""
    confirm_password = request.form.get("confirm_password") or ""

    validation_error = _registration_validation_error(
        name, email, password, confirm_password
    )
    if validation_error:
        return render_template(
            "register.html",
            register_error=validation_error,
            name=name,
            email=email
        ), 400

    password_hash = generate_password_hash(password)
    user_id = _create_registered_user(name, email, password_hash)
    session.clear()
    session.permanent = True
    session["user_id"] = user_id
    session["user_name"] = name
    session["role"] = "owner"
    return redirect("/pricing")
  except mysql.connector.IntegrityError as error:
     if error.errno == 1062:
        current_app.logger.info(
            "Registration rejected because normalized email already exists"
        )
        return render_template(
            "register.html",
            register_error="An account with this email already exists.",
            name=name if "name" in locals() else "",
            email=email if "email" in locals() else ""
        ), 409
     current_app.logger.error(
         "Registration database failure error_type=%s db_errno=%s",
         type(error).__name__,
         error.errno
     )
     return render_template(
         "register.html",
         register_error="We could not create your account right now. Please try again.",
         name=name if "name" in locals() else "",
         email=email if "email" in locals() else ""
     ), 500
  except Exception as error:
     current_app.logger.error(
         "Registration failure error_type=%s",
         type(error).__name__
     )
     return render_template(
         "register.html",
         register_error="We could not create your account right now. Please try again.",
         name=name if "name" in locals() else "",
         email=email if "email" in locals() else ""
     ), 500

#LOGIN PAGE ROUTE AUTHENTICATION

@auth_bp.route("/login-page", methods=["GET"])
def login_page():

    return render_template(
        "login.html"
    )
    
@auth_bp.route("/login-page", methods=["POST"])
def login_form():

    try:

        email = normalize_login_email(request.form.get("email"))
        password = request.form.get("password")
        ip_address = get_client_ip()
        audit = _get_security_audit()
        email_identity_valid = bool(
            email
            and len(email) <= 254
            and validate_login_input(email, "valid")
        )

        limiter = _get_login_limiter()
        try:
            ip_status = limiter.check_ip(ip_address)
        except Exception:
            return _limiter_unavailable_response(
                audit, email, ip_address,
                account_valid=email_identity_valid,
                failure_category="ip_check",
            )
        if ip_status.blocked:
            return _throttled_login_response(
                ip_status.retry_after_seconds, audit, email, ip_address,
                account_valid=email_identity_valid, limiter_scope="ip",
            )

        if not validate_login_csrf():
            audit.emit(
                "login_csrf_rejected",
                email=email,
                account_valid=email_identity_valid,
                client_ip=ip_address,
                http_status=403,
            )
            return _login_error_response(
                LOGIN_CSRF_ERROR_MESSAGE,
                status_code=403,
            )

        input_failure = login_input_failure(email, password)
        input_is_valid = input_failure is None

        recaptcha_result = verify_recaptcha(
            request.form.get("recaptcha_token"),
            "login",
            ip_address
        )
        if not recaptcha_result.success:
            audit.emit(
                "login_recaptcha_rejected",
                email=email,
                account_valid=email_identity_valid,
                client_ip=ip_address,
                failure_category=recaptcha_failure_category(
                    getattr(recaptcha_result, "reason", "")
                ),
                http_status=400,
            )
            return _login_error_response(
                RECAPTCHA_ERROR_MESSAGE,
                email=email,
                status_code=400
            )

        if input_is_valid:
            try:
                account_statuses = limiter.check_account_and_pair(email, ip_address)
            except Exception:
                return _limiter_unavailable_response(
                    audit, email, ip_address, account_valid=True,
                    failure_category="account_check",
                )
            retry_after = longest_retry_after(account_statuses)
            if retry_after:
                blocked_scopes = []
                if account_statuses[0].blocked:
                    blocked_scopes.append("account")
                if account_statuses[1].blocked:
                    blocked_scopes.append("ip_account")
                limiter_scope = (
                    blocked_scopes[0] if len(blocked_scopes) == 1 else "multiple"
                )
                return _throttled_login_response(
                    retry_after, audit, email, ip_address,
                    account_valid=True, limiter_scope=limiter_scope,
                )

        if not input_is_valid:
            email_is_safe = bool(
                email
                and len(email) <= 254
                and validate_login_input(email, "valid")
            )
            try:
                if email_is_safe:
                    limiter.record_failure(email, ip_address)
                else:
                    limiter.record_ip_failure(ip_address)
            except Exception:
                return _limiter_unavailable_response(
                    audit, email, ip_address,
                    account_valid=email_is_safe,
                    failure_category="failure_recording",
                )

            audit.emit(
                "login_input_rejected",
                email=email,
                account_valid=email_is_safe,
                client_ip=ip_address,
                failure_category="_".join(input_failure),
                http_status=401,
            )

            return _login_error_response(
                INVALID_LOGIN_MESSAGE,
                email=email if email_is_safe else ""
            )

        try:
            user = _find_login_user(email)
        except Exception:
            return _login_application_error_response(
                audit, email, ip_address, account_valid=True,
                unavailable=True, failure_category="user_lookup",
            )

        password_hash = (
            user.get("password_hash") if user
            else current_app.config["LOGIN_DUMMY_PASSWORD_HASH"]
        )
        try:
            password_matches = check_password_hash(password_hash, password)
        except Exception:
            return _login_application_error_response(
                audit, email, ip_address, account_valid=True,
                failure_category="password_verification",
            )

        if not user or not password_matches:
            try:
                limiter.record_failure(email, ip_address)
            except Exception:
                return _limiter_unavailable_response(
                    audit, email, ip_address, account_valid=True,
                    failure_category="failure_recording",
                )

            audit.emit(
                "login_invalid_credentials",
                email=email,
                account_valid=True,
                client_ip=ip_address,
                http_status=401,
            )

            return _login_error_response(
                INVALID_LOGIN_MESSAGE,
                email=email
            )

        try:
            limiter.reset_after_success(email, ip_address)
        except Exception:
            return _limiter_unavailable_response(
                audit, email, ip_address, account_valid=True,
                failure_category="success_reset",
            )

        # Discard all pre-authentication state before creating a fresh session.
        session.clear()
        session.permanent = True
        session["user_id"] = user["id"]
        session["user_name"] = user["name"]
        session["role"] = user.get("role") or "owner"

        # Admin users go directly to Admin Dashboard
        if session["role"] == "admin":
            audit.emit(
                "login_success", email=email, client_ip=ip_address, http_status=302
            )
            return redirect("/admin/dashboard")

        try:
            if not has_active_subscription(session["user_id"]):
                audit.emit(
                    "login_success", email=email, client_ip=ip_address, http_status=302
                )
                return redirect("/pricing")

            # Normal owners continue existing flow.
            business = _find_user_business(session["user_id"])
        except Exception:
            session.clear()
            return _login_application_error_response(
                audit, email, ip_address, account_valid=True,
                unavailable=True, failure_category="post_auth_lookup",
            )

        if business:
            audit.emit(
                "login_success", email=email, client_ip=ip_address, http_status=302
            )
            return redirect("/my-businesses")

        audit.emit(
            "login_success", email=email, client_ip=ip_address, http_status=302
        )
        return redirect("/create-business")

    except Exception:
        if "user_id" in session:
            session.clear()
        audit = locals().get("audit") or _get_security_audit()
        return _login_application_error_response(
            audit,
            locals().get("email", ""),
            locals().get("ip_address", "unknown"),
            account_valid=locals().get("email_identity_valid", False),
            failure_category="unexpected_failure",
        )

    
#LOGOUT ROUTE AUTHENTICATION

@auth_bp.route("/account/delete", methods=["DELETE"])
def delete_account():
    if "user_id" not in session:
        return jsonify({
            "success": False,
            "message": "Login required"
        }), 401

    user_id = session["user_id"]
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        conn.start_transaction()

        cursor.execute(
            """
            SELECT email
            FROM users
            WHERE id=%s
            """,
            (user_id,)
        )
        user = cursor.fetchone()

        if not user:
            conn.commit()
            session.clear()
            return jsonify({
                "success": True,
                "message": "Account deleted successfully",
                "redirect": "/"
            })

        # Delete child records first to avoid foreign key errors.
        cursor.execute(
            """
            DELETE FROM google_business_performance
            WHERE business_id IN (
                SELECT id
                FROM businesses
                WHERE user_id=%s
            )
            """,
            (user_id,)
        )
        cursor.execute(
            """
            DELETE FROM google_business_connections
            WHERE business_id IN (
                SELECT id
                FROM businesses
                WHERE user_id=%s
            )
            """,
            (user_id,)
        )
        cursor.execute(
            """
            DELETE FROM reports
            WHERE business_id IN (
                SELECT id
                FROM businesses
                WHERE user_id=%s
            )
            """,
            (user_id,)
        )
        cursor.execute(
            """
            DELETE FROM reviews
            WHERE business_id IN (
                SELECT id
                FROM businesses
                WHERE user_id=%s
            )
            """,
            (user_id,)
        )
        cursor.execute(
            """
            DELETE FROM payments
            WHERE user_id=%s
            """,
            (user_id,)
        )
        cursor.execute(
            """
            DELETE FROM subscriptions
            WHERE user_id=%s
            """,
            (user_id,)
        )
        cursor.execute(
            """
            DELETE FROM businesses
            WHERE user_id=%s
            """,
            (user_id,)
        )
        cursor.execute(
            """
            DELETE FROM login_attempts
            WHERE email=%s
            """,
            (user["email"],)
        )
        cursor.execute(
            """
            DELETE FROM users
            WHERE id=%s
            """,
            (user_id,)
        )

        conn.commit()
        session.clear()

        return jsonify({
            "success": True,
            "message": "Account deleted successfully",
            "redirect": "/"
        })
    except Exception:
        conn.rollback()
        current_app.logger.exception("Account deletion failed for user_id=%s", user_id)

        return jsonify({
            "success": False,
            "message": "Account deletion failed. Please try again."
        }), 500
    finally:
        cursor.close()
        conn.close()
    
@auth_bp.route("/logout", methods=["POST"])
def logout():

    session.clear()

    return redirect(
        "/login-page"
    )
    
        
