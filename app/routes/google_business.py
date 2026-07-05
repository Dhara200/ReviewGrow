from datetime import datetime, timedelta

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session
)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import Config
from app.services.database_service import get_connection, user_owns_business
from app.services.google_business_service import (
    GoogleBusinessError,
    GoogleQuotaError,
    build_oauth_url,
    exchange_code_for_tokens,
    fetch_google_account_profile,
    list_all_locations,
    post_review_reply,
    refresh_access_token
)
from app.services.google_performance_service import (
    load_performance_data,
    parse_date_range,
    sync_performance_metrics
)
from app.services.review_sync_service import sync_google_reviews
from app.services.analysis_job_service import create_analysis_job
from app.services.ai_service import AIService, AIServiceError, log_ai_usage
from app.services.oauth_identity_service import (
    OAUTH_EMAIL_MISMATCH_MESSAGE,
    normalize_email,
    validate_oauth_email
)
from app.services.subscription_service import subscription_required
from app.services.token_crypto_service import decrypt_token, encrypt_token


google_business_bp = Blueprint("google_business", __name__)
REVIEW_SYNC_COOLDOWN_SECONDS = 120
LOCATION_CACHE_SECONDS = 600


def _serializer():
    return URLSafeTimedSerializer(Config.SECRET_KEY)


def _login_required():
    if "user_id" not in session:
        return redirect("/login-page")

    return None


def _business_guard(business_id):
    login_response = _login_required()

    if login_response:
        return login_response

    if not user_owns_business(session["user_id"], business_id):
        return "Access denied", 403

    return None


def _state_for_business(business_id):
    return _serializer().dumps({
        "business_id": business_id,
        "user_id": session["user_id"]
    })


def _load_state(state):
    return _serializer().loads(state, max_age=600)


def _token_scopes(tokens):
    return tokens.get("scope") or Config.GOOGLE_SCOPES


def _clear_oauth_session():
    session.pop("google_oauth_state", None)
    session.pop("google_oauth_business_id", None)
    session.pop("google_oauth_reconnect", None)


def _registered_user():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT id, name, email
        FROM users
        WHERE id=%s
        LIMIT 1
        """,
        (session["user_id"],)
    )
    user = cursor.fetchone()
    cursor.close()
    conn.close()
    return user


def _log_google_oauth_attempt(business_id, registered_email, google_email, status, message=None):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO google_oauth_attempt_logs
        (
            user_id,
            business_id,
            registered_email,
            google_email,
            status,
            message
        )
        VALUES (%s,%s,%s,%s,%s,%s)
        """,
        (
            session["user_id"],
            business_id,
            registered_email,
            google_email,
            status,
            message
        )
    )
    conn.commit()
    cursor.close()
    conn.close()


def _log_admin_gbp_override(business_id, google_email):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO admin_gbp_override_logs
        (
            admin_user_id,
            business_id,
            connected_google_email,
            action
        )
        VALUES (%s,%s,%s,'ADMIN_GBP_OVERRIDE')
        """,
        (
            session["user_id"],
            business_id,
            google_email
        )
    )
    conn.commit()
    cursor.close()
    conn.close()


def _decrypt_connection_tokens(connection):
    if not connection:
        return connection

    connection["access_token"] = decrypt_token(connection.get("access_token"))
    connection["refresh_token"] = decrypt_token(connection.get("refresh_token"))
    return connection


def _connection_email_matches_session_user(connection):
    if not connection or session.get("role") == "admin":
        return True

    google_email = (
        connection.get("google_email")
        or connection.get("google_account_email")
    )
    user = _registered_user()
    registered_email = user.get("email") if user else None

    if not google_email or not registered_email:
        return False

    return normalize_email(google_email) == normalize_email(registered_email)


def _admin_override_active_for_connection(connection):
    if not connection or session.get("role") != "admin":
        return False

    google_email = (
        connection.get("google_email")
        or connection.get("google_account_email")
    )
    user = _registered_user()
    registered_email = user.get("email") if user else None

    return (
        bool(google_email)
        and bool(registered_email)
        and normalize_email(google_email) != normalize_email(registered_email)
    )


def _save_pending_connection(business_id, tokens):
    conn = get_connection()
    cursor = conn.cursor()
    google_account_email = tokens.get("google_account_email")
    google_oauth_account_id = tokens.get("google_oauth_account_id")
    scopes = _token_scopes(tokens)

    cursor.execute(
        """
        INSERT INTO google_business_connections
        (
            user_id,
            business_id,
            google_account_email,
            google_email,
            google_oauth_account_id,
            access_token,
            refresh_token,
            token_expiry,
            scope,
            scopes,
            connection_status,
            is_connected,
            connected_at,
            disconnected_at
        )
        VALUES
        (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NULL)
        ON DUPLICATE KEY UPDATE
            user_id=VALUES(user_id),
            google_account_email=VALUES(google_account_email),
            google_email=VALUES(google_email),
            google_oauth_account_id=VALUES(google_oauth_account_id),
            access_token=VALUES(access_token),
            refresh_token=COALESCE(VALUES(refresh_token), refresh_token),
            token_expiry=VALUES(token_expiry),
            scope=VALUES(scope),
            scopes=VALUES(scopes),
            connection_status='connected',
            is_connected=TRUE,
            connected_at=NOW(),
            disconnected_at=NULL
        """,
        (
            session["user_id"],
            business_id,
            google_account_email,
            google_account_email,
            google_oauth_account_id,
            encrypt_token(tokens.get("access_token")),
            encrypt_token(tokens.get("refresh_token")),
            tokens.get("token_expiry"),
            scopes,
            scopes,
            "connected",
            True
        )
    )

    conn.commit()
    cursor.close()
    conn.close()


def _save_connected_location(business_id, location):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE google_business_connections
        SET
            google_account_id=%s,
            google_location_id=%s,
            google_location_name=%s,
            connection_status='connected',
            is_connected=TRUE
        WHERE business_id=%s
        AND user_id=%s
        """,
        (
            location["account_id"],
            location["location_id"],
            location["location_name"],
            business_id,
            session["user_id"]
        )
    )

    conn.commit()
    cursor.close()
    conn.close()


def _location_cache_key(business_id):
    return f"google_locations:{business_id}"


def _cache_locations(business_id, locations):
    session[_location_cache_key(business_id)] = {
        "cached_at": datetime.utcnow().timestamp(),
        "locations": [
            {
                "account_id": location["account_id"],
                "location_id": location["location_id"],
                "location_name": location["location_name"]
            }
            for location in locations
        ]
    }


def _cached_locations(business_id):
    cache = session.get(_location_cache_key(business_id))

    if not cache:
        return None

    cached_at = cache.get("cached_at")

    if not cached_at:
        return None

    if datetime.utcnow().timestamp() - cached_at > LOCATION_CACHE_SECONDS:
        session.pop(_location_cache_key(business_id), None)
        return None

    return cache.get("locations") or None


def _clear_location_cache(business_id):
    session.pop(_location_cache_key(business_id), None)


def _get_connection_row(business_id, connected_only=False):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    query = """
        SELECT *
        FROM google_business_connections
        WHERE business_id=%s
    """
    params = [business_id]

    if session.get("role") != "admin":
        query += " AND user_id=%s"
        params.append(session["user_id"])

    if connected_only:
        query += " AND is_connected=TRUE"

    cursor.execute(query, tuple(params))
    row = cursor.fetchone()

    cursor.close()
    conn.close()

    row = _decrypt_connection_tokens(row)

    if row and connected_only and not _connection_email_matches_session_user(row):
        current_app.logger.warning(
            "Blocked Google connection with mismatched email: user_id=%s business_id=%s google_email=%s",
            session.get("user_id"),
            business_id,
            row.get("google_email") or row.get("google_account_email")
        )
        return None

    return row


def _store_refreshed_token(connection, token_data):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE google_business_connections
        SET
            access_token=%s,
            token_expiry=%s,
            scope=COALESCE(%s, scope),
            scopes=COALESCE(%s, scopes)
        WHERE id=%s
        """,
        (
            encrypt_token(token_data["access_token"]),
            token_data["token_expiry"],
            _token_scopes(token_data),
            _token_scopes(token_data),
            connection["id"]
        )
    )

    conn.commit()
    cursor.close()
    conn.close()


def _valid_connection_token(connection):
    expiry = connection.get("token_expiry")

    if expiry and expiry > datetime.utcnow() + timedelta(minutes=5):
        return connection

    token_data = refresh_access_token(connection.get("refresh_token"))
    _store_refreshed_token(connection, token_data)

    connection["access_token"] = token_data["access_token"]
    connection["token_expiry"] = token_data["token_expiry"]

    return connection


def _connection_has_location(connection):
    return bool(
        connection
        and connection.get("google_account_id")
        and connection.get("google_location_id")
    )


def _can_post_replies(connection):
    scopes = connection.get("scopes") or connection.get("scope") or ""
    return "business.manage" in scopes


def _load_google_review_for_owner(review_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    if session.get("role") == "admin":
        cursor.execute(
            """
            SELECT
                r.*,
                b.user_id AS owner_id,
                b.business_name,
                b.business_type,
                b.city,
                b.state,
                b.country,
                b.use_reviewer_name,
                b.reply_tone,
                b.max_reply_words,
                b.auto_post_replies
            FROM reviews r
            JOIN businesses b
                ON b.id = r.business_id
            WHERE r.id=%s
            AND r.source='google'
            LIMIT 1
            """,
            (review_id,)
        )
    else:
        cursor.execute(
            """
            SELECT
                r.*,
                b.user_id AS owner_id,
                b.business_name,
                b.business_type,
                b.city,
                b.state,
                b.country,
                b.use_reviewer_name,
                b.reply_tone,
                b.max_reply_words,
                b.auto_post_replies
            FROM reviews r
            JOIN businesses b
                ON b.id = r.business_id
            WHERE r.id=%s
            AND r.source='google'
            AND b.user_id=%s
            LIMIT 1
            """,
            (review_id, session["user_id"])
        )

    review = cursor.fetchone()
    cursor.close()
    conn.close()
    return review


def _business_context_from_review(review):
    return {
        "business_name": review.get("business_name"),
        "business_type": review.get("business_type"),
        "city": review.get("city"),
        "state": review.get("state"),
        "country": review.get("country"),
    }


def _reply_settings_from_review(review):
    return {
        "use_reviewer_name": review.get("use_reviewer_name", True),
        "reply_tone": review.get("reply_tone") or "professional",
        "max_reply_words": review.get("max_reply_words") or 120,
    }


def _resolve_missing_location(business_id, connection):
    if _connection_has_location(connection):
        return connection, None

    connection = _valid_connection_token(connection)
    locations = list_all_locations(connection["access_token"])
    _cache_locations(business_id, locations)

    if len(locations) == 1:
        _save_connected_location(business_id, locations[0])
        _clear_location_cache(business_id)
        return _get_connection_row(business_id, connected_only=True), None

    flash("Select the Google Business Profile location to sync reviews.", "info")
    return None, redirect(f"/businesses/{business_id}/google/select-location")


def _review_sync_cooldown_key(business_id):
    return f"google_review_sync_started_at:{business_id}"


def _review_sync_cooldown_response(business_id):
    key = _review_sync_cooldown_key(business_id)
    started_at = session.get(key)

    if not started_at:
        return None

    elapsed = datetime.utcnow().timestamp() - started_at

    if elapsed >= REVIEW_SYNC_COOLDOWN_SECONDS:
        return None

    remaining = int(REVIEW_SYNC_COOLDOWN_SECONDS - elapsed)
    flash(
        f"Google review sync is cooling down. Try again in about {remaining} seconds.",
        "warning"
    )
    return redirect(f"/businesses/{business_id}/live-dashboard")


def _start_review_sync_cooldown(business_id):
    session[_review_sync_cooldown_key(business_id)] = datetime.utcnow().timestamp()


def _clear_review_sync_cooldown(business_id):
    session.pop(_review_sync_cooldown_key(business_id), None)


def _empty_google_review_stats():
    return {
        "total_reviews": 0,
        "average_rating": 0,
        "positive_reviews": 0,
        "neutral_reviews": 0,
        "negative_reviews": 0
    }


def _google_review_stats(business_id, google_location_id=None):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    if google_location_id:
        cursor.execute(
            """
            SELECT
                COUNT(*) AS total_reviews,
                AVG(COALESCE(review_rating, rating)) AS average_rating,
                SUM(CASE WHEN sentiment='Positive' THEN 1 ELSE 0 END) AS positive_reviews,
                SUM(CASE WHEN sentiment='Neutral' THEN 1 ELSE 0 END) AS neutral_reviews,
                SUM(CASE WHEN sentiment='Negative' THEN 1 ELSE 0 END) AS negative_reviews
            FROM reviews
            WHERE business_id=%s
            AND source='google'
            AND google_location_id=%s
            """,
            (business_id, google_location_id)
        )
        stats = cursor.fetchone()

        cursor.execute(
            """
            SELECT
                id,
                reviewer_name,
                review_text,
                COALESCE(review_rating, rating) AS rating,
                review_created_at,
                review_updated_at,
                sentiment,
                summary,
                suggested_reply,
                reply_status,
                reply_generated_at,
                reply_posted_at,
                reply_error_message,
                google_review_id,
                external_review_id
            FROM reviews
            WHERE business_id=%s
            AND source='google'
            AND google_location_id=%s
            ORDER BY COALESCE(review_updated_at, review_created_at, review_date, created_at) DESC
            LIMIT 25
            """,
            (business_id, google_location_id)
        )
        reviews = cursor.fetchall()
    else:
        stats = _empty_google_review_stats()
        reviews = []

    if session.get("role") == "admin":
        cursor.execute(
            """
            SELECT
                business_name,
                business_type,
                city,
                state,
                country,
                use_reviewer_name,
                reply_tone,
                max_reply_words,
                auto_generate_replies_for_new_reviews,
                auto_post_replies
            FROM businesses
            WHERE id=%s
            """,
            (business_id,)
        )
    else:
        cursor.execute(
            """
            SELECT
                business_name,
                business_type,
                city,
                state,
                country,
                use_reviewer_name,
                reply_tone,
                max_reply_words,
                auto_generate_replies_for_new_reviews,
                auto_post_replies
            FROM businesses
            WHERE id=%s
            AND user_id=%s
            """,
            (business_id, session["user_id"])
        )
    business = cursor.fetchone()

    cursor.close()
    conn.close()

    return business, stats, reviews


@google_business_bp.route("/businesses/<int:business_id>/google/connect")
@google_business_bp.route("/auth/google/start/<int:business_id>")
@subscription_required
def connect_google_business(business_id):
    guard = _business_guard(business_id)

    if guard:
        _clear_oauth_session()
        return guard

    try:
        existing_connection = _get_connection_row(business_id, connected_only=True)
        reconnect_confirmed = request.args.get("reconnect") == "1"

        if existing_connection and not reconnect_confirmed:
            flash(
                (
                    "Google Business Profile is already connected. Use Reconnect "
                    "Google Account if you want to replace it."
                ),
                "warning"
            )
            return redirect(f"/businesses/{business_id}/live-dashboard")

        state = _state_for_business(business_id)
        session["google_oauth_state"] = state
        session["google_oauth_business_id"] = business_id
        session["google_oauth_reconnect"] = reconnect_confirmed
        return redirect(build_oauth_url(state))
    except GoogleBusinessError as e:
        flash(str(e), "danger")
        return redirect("/my-businesses")


@google_business_bp.route("/google/oauth/callback")
@google_business_bp.route("/auth/google/callback")
def google_oauth_callback():
    login_response = _login_required()

    if login_response:
        return login_response

    if request.args.get("error"):
        _clear_oauth_session()
        flash("Google OAuth was denied or cancelled.", "warning")
        return redirect("/my-businesses")

    state = request.args.get("state")
    code = request.args.get("code")

    if not state or not code:
        _clear_oauth_session()
        flash("Google OAuth response was incomplete. Please try again.", "danger")
        return redirect("/my-businesses")

    if state != session.get("google_oauth_state"):
        _clear_oauth_session()
        flash("Google OAuth state validation failed. Please try again.", "danger")
        return redirect("/my-businesses")

    try:
        state_data = _load_state(state)
    except SignatureExpired:
        _clear_oauth_session()
        flash("Google OAuth session expired. Please try connecting again.", "warning")
        return redirect("/my-businesses")
    except BadSignature:
        _clear_oauth_session()
        flash("Google OAuth state validation failed. Please try again.", "danger")
        return redirect("/my-businesses")

    business_id = int(state_data["business_id"])

    if state_data.get("user_id") != session["user_id"]:
        _clear_oauth_session()
        flash("Google OAuth state does not match this user.", "danger")
        return redirect("/my-businesses")

    if business_id != session.get("google_oauth_business_id"):
        _clear_oauth_session()
        flash("Google OAuth business selection does not match this session.", "danger")
        return redirect("/my-businesses")

    guard = _business_guard(business_id)

    if guard:
        _clear_oauth_session()
        return guard

    try:
        tokens = exchange_code_for_tokens(code)
        google_profile = fetch_google_account_profile(tokens["access_token"])
        google_email = google_profile.get("email")
        google_email_verified = google_profile.get("email_verified") is True
        tokens["google_account_email"] = google_email
        tokens["google_oauth_account_id"] = google_profile.get("google_oauth_account_id")

        registered_user = _registered_user()
        registered_email = registered_user.get("email") if registered_user else None

        if not registered_user or not registered_email:
            _clear_oauth_session()
            flash("Your registered account email could not be verified. Please log in again.", "danger")
            return redirect("/login-page")

        if not google_email:
            _log_google_oauth_attempt(
                business_id,
                registered_email,
                None,
                "missing_google_email",
                "Google userinfo did not return an email."
            )
            _clear_oauth_session()
            flash(
                (
                    "Google did not return your account email. Please try again "
                    "and make sure email permission is allowed."
                ),
                "danger"
            )
            return redirect(f"/businesses/{business_id}/live-dashboard")

        if not google_email_verified:
            _log_google_oauth_attempt(
                business_id,
                registered_email,
                google_email,
                "unverified_google_email",
                "Google userinfo returned an unverified email."
            )
            _clear_oauth_session()
            flash(
                "Your Google account email is not verified. Please use a verified Google account.",
                "danger"
            )
            return redirect(f"/businesses/{business_id}/live-dashboard")

        validation = validate_oauth_email(
            registered_email,
            google_email,
            session.get("role")
        )

        if not validation.allowed:
            _log_google_oauth_attempt(
                business_id,
                registered_email,
                google_email,
                "email_mismatch",
                "Google OAuth email did not match registered SaaS email."
            )
            _clear_oauth_session()
            return render_template(
                "google_oauth_mismatch.html",
                business_id=business_id,
                registered_email=registered_email,
                google_email=google_email,
                message=validation.message or OAUTH_EMAIL_MISMATCH_MESSAGE
            ), 403

        _save_pending_connection(business_id, tokens)
        if validation.admin_override:
            _log_admin_gbp_override(business_id, google_email)

        _log_google_oauth_attempt(
            business_id,
            registered_email,
            google_email,
            "admin_override" if validation.admin_override else "connected",
            (
                "Admin override allowed a different Google account."
                if validation.admin_override else
                "Google OAuth email matched registered SaaS email."
            )
        )
        _clear_oauth_session()

        try:
            locations = list_all_locations(tokens["access_token"])
            _cache_locations(business_id, locations)
        except GoogleBusinessError as e:
            flash(
                (
                    "Google account connected, but ReviewSense could not load a "
                    f"Business Profile location yet: {e}"
                ),
                "warning"
            )
            return redirect(f"/business/{business_id}/live-dashboard")

        if len(locations) == 1:
            _save_connected_location(business_id, locations[0])
            _clear_location_cache(business_id)
            flash("Google Business Profile connected successfully.", "success")
            return redirect(f"/business/{business_id}/live-dashboard")

        flash("Select the Google Business Profile location to connect.", "info")
        return redirect(f"/businesses/{business_id}/google/select-location")
    except GoogleBusinessError as e:
        _clear_oauth_session()
        flash(str(e), "danger")
        return redirect("/my-businesses")
    except Exception:
        _clear_oauth_session()
        current_app.logger.exception("Google OAuth callback failed")
        flash("Google connection failed. Please try again.", "danger")
        return redirect("/my-businesses")


@google_business_bp.route("/businesses/<int:business_id>/google/select-location")
@subscription_required
def select_google_location_page(business_id):
    guard = _business_guard(business_id)

    if guard:
        return guard

    connection = _get_connection_row(business_id)

    if not connection:
        flash("Start Google connection before selecting a location.", "warning")
        return redirect("/my-businesses")

    try:
        connection = _valid_connection_token(connection)
        locations = _cached_locations(business_id)

        if not locations:
            locations = list_all_locations(connection["access_token"])
            _cache_locations(business_id, locations)

        return render_template(
            "select_google_location.html",
            business_id=business_id,
            locations=locations,
            admin_override_active=_admin_override_active_for_connection(connection)
        )
    except GoogleQuotaError as e:
        flash(
            (
                f"{e} This is an API quota/rate limit, not a Google Cloud billing-credit issue. "
                "Avoid refreshing this page repeatedly and check the enabled API quotas in Google Cloud Console."
            ),
            "warning"
        )
        return redirect(f"/business/{business_id}/live-dashboard")
    except GoogleBusinessError as e:
        flash(str(e), "danger")
        return redirect("/my-businesses")


@google_business_bp.route("/businesses/<int:business_id>/google/select-location", methods=["POST"])
@subscription_required
def select_google_location_submit(business_id):
    guard = _business_guard(business_id)

    if guard:
        return guard

    selected_location_id = request.form.get("location_id")

    if not selected_location_id:
        flash("Please select a Google Business Profile location.", "warning")
        return redirect(f"/businesses/{business_id}/google/select-location")

    connection = _get_connection_row(business_id)

    if not connection:
        flash("Google connection was not found. Please reconnect.", "danger")
        return redirect("/my-businesses")

    try:
        connection = _valid_connection_token(connection)
        locations = _cached_locations(business_id)

        if not locations:
            locations = list_all_locations(connection["access_token"])
            _cache_locations(business_id, locations)

        selected = next(
            (
                location
                for location in locations
                if location["location_id"] == selected_location_id
            ),
            None
        )

        if not selected:
            flash("Selected Google location was not found.", "danger")
            return redirect(f"/businesses/{business_id}/google/select-location")

        _save_connected_location(business_id, selected)
        _clear_location_cache(business_id)
        flash("Google Business Profile connected successfully.", "success")
        return redirect(f"/businesses/{business_id}/live-dashboard")
    except GoogleQuotaError as e:
        flash(
            (
                f"{e} This is an API quota/rate limit, not a Google Cloud billing-credit issue. "
                "Try again after the cooldown or increase the relevant Business Profile API quota."
            ),
            "warning"
        )
        return redirect(f"/business/{business_id}/live-dashboard")
    except GoogleBusinessError as e:
        flash(str(e), "danger")
        return redirect("/my-businesses")


@google_business_bp.route("/businesses/<int:business_id>/google/disconnect", methods=["POST"])
@subscription_required
def disconnect_google_business(business_id):
    guard = _business_guard(business_id)

    if guard:
        return guard

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE google_business_connections
        SET
            access_token=NULL,
            refresh_token=NULL,
            token_expiry=NULL,
            google_account_id=NULL,
            google_location_id=NULL,
            google_location_name=NULL,
            connection_status='disconnected',
            is_connected=FALSE,
            disconnected_at=NOW()
        WHERE business_id=%s
        AND user_id=%s
        """,
        (business_id, session["user_id"])
    )
    conn.commit()
    cursor.close()
    conn.close()
    _clear_location_cache(business_id)

    flash("Google Business Profile disconnected.", "success")
    return redirect(f"/businesses/{business_id}/live-dashboard")


@google_business_bp.route("/businesses/<int:business_id>/live-dashboard")
@google_business_bp.route("/business/<int:business_id>/live-dashboard")
@subscription_required
def live_dashboard(business_id):
    guard = _business_guard(business_id)

    if guard:
        return guard

    active_tab = request.args.get("tab", "reviews")

    if active_tab not in {"reviews", "performance"}:
        active_tab = "reviews"

    connection = _get_connection_row(business_id, connected_only=True)
    can_post_replies = _can_post_replies(connection) if connection else False
    google_location_id = (
        connection.get("google_location_id")
        if _connection_has_location(connection)
        else None
    )
    business, stats, reviews = _google_review_stats(
        business_id,
        google_location_id
    )
    performance = None
    needs_location = bool(connection and not _connection_has_location(connection))
    try:
        performance_start, performance_end = parse_date_range(
            request.args.get("start_date"),
            request.args.get("end_date")
        )
    except GoogleBusinessError as e:
        flash(str(e), "warning")
        performance_start, performance_end = parse_date_range()

    if connection and active_tab == "performance":
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        performance_user_id = (
            connection["user_id"]
            if session.get("role") == "admin"
            else session["user_id"]
        )
        performance = load_performance_data(
            cursor,
            business_id,
            performance_user_id,
            performance_start,
            performance_end
        )
        cursor.close()
        conn.close()

    return render_template(
        "live_dashboard.html",
        business_id=business_id,
        business=business,
        connection=connection,
        needs_location=needs_location,
        admin_override_active=_admin_override_active_for_connection(connection),
        stats=stats,
        reviews=reviews,
        active_tab=active_tab,
        performance=performance,
        performance_start=performance_start,
        performance_end=performance_end,
        can_post_replies=can_post_replies
    )


@google_business_bp.route("/businesses/<int:business_id>/google/sync-reviews", methods=["POST"])
@subscription_required
def sync_google_business_reviews(business_id):
    guard = _business_guard(business_id)

    if guard:
        return guard

    connection = _get_connection_row(business_id, connected_only=True)

    if not connection:
        flash("Google Business Profile is not connected.", "warning")
        return redirect(f"/businesses/{business_id}/live-dashboard")

    try:
        cooldown_response = _review_sync_cooldown_response(business_id)

        if cooldown_response:
            return cooldown_response

        _start_review_sync_cooldown(business_id)
        connection = _valid_connection_token(connection)
        connection, location_response = _resolve_missing_location(
            business_id,
            connection
        )

        if location_response:
            _clear_review_sync_cooldown(business_id)
            return location_response

        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        result = sync_google_reviews(cursor, connection)

        cursor.execute(
            """
            UPDATE google_business_connections
            SET last_sync_at=NOW()
            WHERE id=%s
            """,
            (connection["id"],)
        )

        conn.commit()
        cursor.close()
        conn.close()

        job_id, created = create_analysis_job(
            session["user_id"],
            business_id,
            force_reanalysis=False
        )
        _clear_review_sync_cooldown(business_id)

        job_message = (
            f" AI analysis job #{job_id} queued."
            if created else
            f" Existing AI analysis job #{job_id} is still running."
        )
        flash(
            (
                f"Google sync complete: {result['inserted_count']} new, "
                f"{result['updated_count']} updated.{job_message}"
            ),
            "success"
        )
    except GoogleQuotaError as e:
        flash(str(e), "warning")
    except GoogleBusinessError as e:
        _clear_review_sync_cooldown(business_id)
        flash(str(e), "danger")
    except Exception:
        _clear_review_sync_cooldown(business_id)
        current_app.logger.exception("Google review sync failed")
        flash("Google review sync failed. Please try again.", "danger")

    return redirect(f"/businesses/{business_id}/live-dashboard")


@google_business_bp.route("/businesses/<int:business_id>/reply-settings", methods=["POST"])
@subscription_required
def update_reply_settings(business_id):
    guard = _business_guard(business_id)

    if guard:
        return guard

    allowed_tones = {"professional", "friendly", "luxury", "casual"}
    reply_tone = request.form.get("reply_tone") or "professional"

    if reply_tone not in allowed_tones:
        reply_tone = "professional"

    try:
        max_reply_words = int(request.form.get("max_reply_words") or 120)
    except ValueError:
        max_reply_words = 120

    max_reply_words = min(max(max_reply_words, 40), 200)

    conn = get_connection()
    cursor = conn.cursor()
    query = """
        UPDATE businesses
        SET use_reviewer_name=%s,
            reply_tone=%s,
            max_reply_words=%s,
            auto_generate_replies_for_new_reviews=%s,
            auto_post_replies=%s
        WHERE id=%s
    """
    params = [
        request.form.get("use_reviewer_name") == "1",
        reply_tone,
        max_reply_words,
        request.form.get("auto_generate_replies_for_new_reviews") == "1",
        request.form.get("auto_post_replies") == "1",
        business_id,
    ]
    if session.get("role") != "admin":
        query += " AND user_id=%s"
        params.append(session["user_id"])

    cursor.execute(query, tuple(params))
    conn.commit()
    cursor.close()
    conn.close()

    flash("Reply personalization settings saved.", "success")
    return redirect(f"/businesses/{business_id}/live-dashboard")


@google_business_bp.route("/reviews/<int:review_id>/reply/regenerate", methods=["POST"])
@subscription_required
def regenerate_google_review_reply(review_id):
    if "user_id" not in session:
        return jsonify({"message": "Login required"}), 401

    review = _load_google_review_for_owner(review_id)

    if not review:
        return jsonify({"message": "Google review not found"}), 404

    if not (review.get("review_text") or "").strip():
        return jsonify({"message": "Review text is empty."}), 400

    ai_service = AIService()
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        result = ai_service.generate_google_review_reply(
            review,
            _business_context_from_review(review),
            settings=_reply_settings_from_review(review)
        )
        log_ai_usage(cursor, review["owner_id"], review["business_id"], result)
        suggested_reply = result.data.get("reply", "").strip()

        cursor.execute(
            """
            UPDATE reviews
            SET suggested_reply=%s,
                ai_reply=%s,
                reply_status='pending',
                reply_generated_at=NOW(),
                reply_error_message=NULL
            WHERE id=%s
            """,
            (suggested_reply, suggested_reply, review_id)
        )
        conn.commit()
    except AIServiceError as error:
        conn.rollback()
        failed_result = error.result
        if failed_result:
            log_ai_usage(cursor, review["owner_id"], review["business_id"], failed_result)
            conn.commit()
        return jsonify({"message": str(error)}), 500
    finally:
        cursor.close()
        conn.close()

    return jsonify({
        "success": True,
        "suggested_reply": suggested_reply,
        "reply_status": "pending"
    })


@google_business_bp.route("/reviews/<int:review_id>/reply/approve", methods=["POST"])
@subscription_required
def approve_google_review_reply(review_id):
    if "user_id" not in session:
        return jsonify({"message": "Login required"}), 401

    review = _load_google_review_for_owner(review_id)

    if not review:
        return jsonify({"message": "Google review not found"}), 404

    suggested_reply = (request.form.get("suggested_reply") or "").strip()
    if request.is_json:
        suggested_reply = (
            (request.get_json(silent=True) or {}).get("suggested_reply") or ""
        ).strip()
    suggested_reply = suggested_reply or (review.get("suggested_reply") or "").strip()

    if not suggested_reply:
        return jsonify({"message": "Generate a reply before approving it."}), 400

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE reviews
        SET suggested_reply=%s,
            ai_reply=%s,
            reply_status='approved',
            reply_error_message=NULL
        WHERE id=%s
        """,
        (suggested_reply, suggested_reply, review_id)
    )
    conn.commit()
    cursor.close()
    conn.close()

    return jsonify({
        "success": True,
        "suggested_reply": suggested_reply,
        "reply_status": "approved"
    })


@google_business_bp.route("/reviews/<int:review_id>/reply/post", methods=["POST"])
@subscription_required
def post_google_review_reply_route(review_id):
    if "user_id" not in session:
        return jsonify({"message": "Login required"}), 401

    review = _load_google_review_for_owner(review_id)

    if not review:
        return jsonify({"message": "Google review not found"}), 404

    if review.get("reply_status") != "approved":
        return jsonify({"message": "Approve the reply before posting to Google."}), 400

    suggested_reply = (review.get("suggested_reply") or "").strip()
    google_review_id = review.get("google_review_id") or review.get("external_review_id")
    google_review_id = google_review_id.rstrip("/").split("/")[-1] if google_review_id else None

    if not suggested_reply:
        return jsonify({"message": "Approved reply text is empty."}), 400

    if not google_review_id:
        return jsonify({"message": "Google review ID is missing."}), 400

    connection = _get_connection_row(review["business_id"], connected_only=True)

    if not connection:
        return jsonify({"message": "Google Business Profile is not connected."}), 400

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        connection = _valid_connection_token(connection)
        post_review_reply(
            connection["access_token"],
            connection["google_account_id"],
            connection["google_location_id"],
            google_review_id,
            suggested_reply
        )
        cursor.execute(
            """
            UPDATE reviews
            SET reply_status='posted',
                reply_posted_at=NOW(),
                reply_error_message=NULL
            WHERE id=%s
            """,
            (review_id,)
        )
        cursor.execute(
            """
            INSERT INTO google_review_reply_logs
            (
                review_id,
                business_id,
                user_id,
                google_review_id,
                reply_text,
                status
            )
            VALUES (%s,%s,%s,%s,%s,'posted')
            """,
            (
                review_id,
                review["business_id"],
                review["owner_id"],
                google_review_id,
                suggested_reply
            )
        )
        conn.commit()
        return jsonify({"success": True, "reply_status": "posted"})
    except GoogleBusinessError as error:
        conn.rollback()
        cursor.execute(
            """
            UPDATE reviews
            SET reply_status='failed',
                reply_error_message=%s
            WHERE id=%s
            """,
            (str(error)[:1000], review_id)
        )
        cursor.execute(
            """
            INSERT INTO google_review_reply_logs
            (
                review_id,
                business_id,
                user_id,
                google_review_id,
                reply_text,
                status,
                error_message
            )
            VALUES (%s,%s,%s,%s,%s,'failed',%s)
            """,
            (
                review_id,
                review["business_id"],
                review["owner_id"],
                google_review_id,
                suggested_reply,
                str(error)[:1000]
            )
        )
        conn.commit()
        return jsonify({"message": str(error), "reply_status": "failed"}), 500
    finally:
        cursor.close()
        conn.close()


@google_business_bp.route("/businesses/<int:business_id>/google/sync-performance", methods=["POST"])
@subscription_required
def sync_google_business_performance(business_id):
    guard = _business_guard(business_id)

    if guard:
        return guard

    connection = _get_connection_row(business_id, connected_only=True)

    if not connection:
        flash("Google Business Profile is not connected.", "warning")
        return redirect(f"/businesses/{business_id}/live-dashboard?tab=performance")

    try:
        start_date, end_date = parse_date_range(
            request.form.get("start_date"),
            request.form.get("end_date")
        )
        connection = _valid_connection_token(connection)
        connection, location_response = _resolve_missing_location(
            business_id,
            connection
        )

        if location_response:
            return location_response

        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        result = sync_performance_metrics(cursor, connection, start_date, end_date)

        conn.commit()
        cursor.close()
        conn.close()

        if result["saved_count"] == 0:
            flash("No Google Business Profile performance data was available for that date range.", "warning")
        else:
            flash(f"Performance sync complete: {result['saved_count']} metric points saved.", "success")

        return redirect(
            f"/businesses/{business_id}/live-dashboard?tab=performance"
            f"&start_date={start_date}&end_date={end_date}"
        )
    except GoogleBusinessError as e:
        flash(str(e), "danger")
    except Exception:
        current_app.logger.exception("Google performance sync failed")
        flash("Google performance sync failed. Please try again.", "danger")

    return redirect(f"/businesses/{business_id}/live-dashboard?tab=performance")
