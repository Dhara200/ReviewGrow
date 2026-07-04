from datetime import datetime, timedelta

from flask import (
    Blueprint,
    current_app,
    flash,
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
    fetch_google_account_email,
    list_all_locations,
    refresh_access_token
)
from app.services.google_performance_service import (
    load_performance_data,
    parse_date_range,
    sync_performance_metrics
)
from app.services.review_sync_service import sync_google_reviews
from app.services.analysis_job_service import create_analysis_job
from app.services.subscription_service import subscription_required


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


def _save_pending_connection(business_id, tokens):
    conn = get_connection()
    cursor = conn.cursor()
    google_account_email = tokens.get("google_account_email")
    scopes = _token_scopes(tokens)

    cursor.execute(
        """
        INSERT INTO google_business_connections
        (
            user_id,
            business_id,
            google_account_email,
            access_token,
            refresh_token,
            token_expiry,
            scope,
            scopes,
            connection_status,
            is_connected
        )
        VALUES
        (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            user_id=VALUES(user_id),
            google_account_email=VALUES(google_account_email),
            access_token=VALUES(access_token),
            refresh_token=COALESCE(VALUES(refresh_token), refresh_token),
            token_expiry=VALUES(token_expiry),
            scope=VALUES(scope),
            scopes=VALUES(scopes),
            connection_status='connected',
            is_connected=TRUE
        """,
        (
            session["user_id"],
            business_id,
            google_account_email,
            tokens.get("access_token"),
            tokens.get("refresh_token"),
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
            token_data["access_token"],
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
                summary
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
            SELECT business_name, business_type, city, state, country
            FROM businesses
            WHERE id=%s
            """,
            (business_id,)
        )
    else:
        cursor.execute(
            """
            SELECT business_name, business_type, city, state, country
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
        return guard

    try:
        state = _state_for_business(business_id)
        session["google_oauth_state"] = state
        session["google_oauth_business_id"] = business_id
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
        flash("Google OAuth was denied or cancelled.", "warning")
        return redirect("/my-businesses")

    state = request.args.get("state")
    code = request.args.get("code")

    if not state or not code:
        flash("Google OAuth response was incomplete. Please try again.", "danger")
        return redirect("/my-businesses")

    if state != session.get("google_oauth_state"):
        flash("Google OAuth state validation failed. Please try again.", "danger")
        return redirect("/my-businesses")

    try:
        state_data = _load_state(state)
    except SignatureExpired:
        flash("Google OAuth session expired. Please try connecting again.", "warning")
        return redirect("/my-businesses")
    except BadSignature:
        flash("Google OAuth state validation failed. Please try again.", "danger")
        return redirect("/my-businesses")

    business_id = int(state_data["business_id"])

    if state_data.get("user_id") != session["user_id"]:
        flash("Google OAuth state does not match this user.", "danger")
        return redirect("/my-businesses")

    if business_id != session.get("google_oauth_business_id"):
        flash("Google OAuth business selection does not match this session.", "danger")
        return redirect("/my-businesses")

    guard = _business_guard(business_id)

    if guard:
        return guard

    try:
        tokens = exchange_code_for_tokens(code)
        tokens["google_account_email"] = fetch_google_account_email(
            tokens["access_token"]
        )
        _save_pending_connection(business_id, tokens)
        session.pop("google_oauth_state", None)
        session.pop("google_oauth_business_id", None)

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
        flash(str(e), "danger")
        return redirect("/my-businesses")
    except Exception:
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
            locations=locations
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
        stats=stats,
        reviews=reviews,
        active_tab=active_tab,
        performance=performance,
        performance_start=performance_start,
        performance_end=performance_end
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
