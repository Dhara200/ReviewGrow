from datetime import datetime, timedelta

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

auth_bp = Blueprint("auth", __name__)
INVALID_LOGIN_MESSAGE = "Invalid email or password."
LOCKED_LOGIN_MESSAGE = "Too many failed login attempts. Please try again after 15 minutes."
RECAPTCHA_ERROR_MESSAGE = "Security verification failed. Please try again."


def get_client_ip():
    forwarded_for = request.headers.get("X-Forwarded-For", "")

    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()

    return request.remote_addr or "unknown"


def _login_error_response(message, email="", status_code=401, locked_until=None):
    lock_seconds = None

    if locked_until:
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
        current_app.logger.warning(
            "Login locked for email=%s ip=%s until=%s",
            email,
            ip_address,
            locked_until
        )

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

    if not all([name, email, password, confirm_password]):
        return render_template(
            "register.html",
            register_error="All fields are required.",
            name=name,
            email=email
        ), 400

    if password != confirm_password:
        return render_template(
            "register.html",
            register_error="Passwords do not match.",
            name=name,
            email=email
        ), 400

    password_hash = generate_password_hash(password)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO users
        (name, email, password_hash)
        VALUES (%s, %s, %s)
        """,
        (
            name,
            email,
            password_hash
        )
    )
    user_id = cursor.lastrowid
    conn.commit()
    cursor.close()
    conn.close()
    create_expired_subscription(user_id)
    session["user_id"] = user_id
    session["user_name"] = name
    session["role"] = "owner"
    return redirect("/pricing")
  except Exception as e:
     return str(e), 500

#LOGIN PAGE ROUTE AUTHENTICATION

@auth_bp.route("/login-page", methods=["GET"])
def login_page():

    return render_template(
        "login.html"
    )
    
@auth_bp.route("/login-page", methods=["POST"])
def login_form():

    try:

        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password")
        ip_address = get_client_ip()

        recaptcha_result = verify_recaptcha(
            request.form.get("recaptcha_token"),
            "login",
            ip_address
        )
        if not recaptcha_result.success:
            return _login_error_response(
                RECAPTCHA_ERROR_MESSAGE,
                email=email,
                status_code=400
            )

        locked_until = is_login_locked(email, ip_address) if email else None

        if locked_until:
            current_app.logger.warning(
                "Blocked locked login attempt for email=%s ip=%s",
                email,
                ip_address
            )
            return _login_error_response(
                LOCKED_LOGIN_MESSAGE,
                email=email,
                status_code=429,
                locked_until=locked_until
            )

        if not email or not password:
            if email:
                record_failed_login(email, ip_address)

            return _login_error_response(
                INVALID_LOGIN_MESSAGE,
                email=email
            )

        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            """
            SELECT *
            FROM users
            WHERE email=%s
            """,
            (email,)
        )

        user = cursor.fetchone()

        cursor.close()
        conn.close()

        if not user:
            record_failed_login(email, ip_address)

            return _login_error_response(
                INVALID_LOGIN_MESSAGE,
                email=email
            )

        if not check_password_hash(
            user["password_hash"],
            password
        ):
            record_failed_login(email, ip_address)

            return _login_error_response(
                INVALID_LOGIN_MESSAGE,
                email=email
            )

        reset_failed_login(email, ip_address)

        # Save login session
        session["user_id"] = user["id"]
        session["user_name"] = user["name"]
        session["role"] = user.get("role") or "owner"

        # Admin users go directly to Admin Dashboard
        if session["role"] == "admin":
            return redirect("/admin/dashboard")

        if not has_active_subscription(session["user_id"]):
            return redirect("/pricing")

        # Normal owners continue existing flow
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            """
            SELECT id
            FROM businesses
            WHERE user_id=%s
            LIMIT 1
            """,
            (session["user_id"],)
        )

        business = cursor.fetchone()

        cursor.close()
        conn.close()

        if business:
            return redirect("/my-businesses")

        return redirect("/create-business")

    except Exception as e:
        return str(e), 500

    
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
    
@auth_bp.route("/logout")
def logout():

    session.clear()

    return redirect(
        "/login-page"
    )
    
        
