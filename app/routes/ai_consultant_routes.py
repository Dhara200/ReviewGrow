import json

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session
from app.config import Config
from app.services.competitor_service import (add_competitor, allow_competitor_search, comparison_summary, get_competitor,
    list_competitors, refresh_allowed, remove_competitor, update_competitor)
from app.services.google_places_service import (PlacesError, discover_competitors, get_place_details,
    clamp_radius, validate_place_id)
from app.services.competitor_refresh_service import (
    JOB_TYPE as COMPETITOR_REFRESH_JOB_TYPE,
    create_competitor_refresh_job,
)
from app.services.competitor_history_analytics_service import build_competitor_history

from app.services.ai_consultant_service import (
    get_latest_consultant_report,
)
from app.services.consultant_analytics_service import (
    build_consultant_data,
    build_legacy_context,
    resolve_period,
)
from app.services.analysis_job_service import (
    create_consultant_job,
    get_active_job_for_business,
)
from app.services.business_analytics_service import (
    get_business_review_metrics,
)
from app.services.consultant_action_service import (
    filter_active_alerts,
    sync_consultant_actions,
    update_consultant_action_status,
)
from app.services.database_service import get_connection, user_owns_business
from app.services.subscription_service import subscription_required


ai_consultant_bp = Blueprint("ai_consultant", __name__)


@ai_consultant_bp.route("/business/<int:business_id>/ai-consultant")
@subscription_required
def ai_consultant_page(business_id):
    if "user_id" not in session:
        return redirect("/login-page")

    if not user_owns_business(session["user_id"], business_id):
        return "Access denied", 403

    business = _load_business(business_id)
    google_connection = _load_google_connection(business_id)
    google_location_id = google_connection.get("google_location_id") if google_connection else None
    report = get_latest_consultant_report(business_id)
    consultant_job = get_active_job_for_business(business_id, "ai_consultant")
    selected_period = resolve_period(request.args.get("period"))["days"]
    consultant_data = build_consultant_data(
        business_id,
        period_value=selected_period,
        google_location_id=google_location_id,
    )
    metrics, command_center = build_legacy_context(
        consultant_data,
        report=report,
        latest_review_at=google_connection.get("last_sync_at") if google_connection else None,
    )
    competitors = list_competitors(business_id)
    competitor_job = get_active_job_for_business(business_id, COMPETITOR_REFRESH_JOB_TYPE)
    if competitor_job and isinstance(competitor_job.get("result_json"), str):
        try:
            competitor_job["result"] = json.loads(competitor_job["result_json"])
        except (TypeError, ValueError):
            competitor_job["result"] = None
    elif competitor_job:
        competitor_job["result"] = competitor_job.get("result_json") if isinstance(competitor_job.get("result_json"), dict) else None
    competitor_history = build_competitor_history(
        business_id, request.args.get("history_window", "available")
    )
    history_by_subject = {
        item["subject_key"]: item for item in competitor_history["subjects"]
    }
    for competitor in competitors:
        competitor["history"] = history_by_subject.get(f"competitor:{competitor['id']}")
    customer_history = history_by_subject.get("customer") or {}
    customer_competitor_metrics = {
        "rating": customer_history.get("current_rating") if customer_history.get("current_rating") is not None else metrics.get("average_rating"),
        "review_count": customer_history.get("current_review_count") if customer_history.get("current_review_count") is not None else metrics.get("total_reviews"),
    }
    competitor_summary = comparison_summary(
        customer_competitor_metrics["rating"], customer_competitor_metrics["review_count"], competitors
    )
    action_state = sync_consultant_actions(
        business_id,
        command_center,
        report_id=report.get("id") if report else None,
    )
    command_center["alerts"] = filter_active_alerts(
        command_center["alerts"],
        action_state,
    )
    can_generate = metrics["total_reviews"] >= 5
    return render_template(
        "ai_consultant.html",
        business=business,
        google_connection=google_connection,
        business_id=business_id,
        metrics=metrics,
        report=report,
        command_center=command_center,
        action_state=action_state,
        can_generate=can_generate,
        consultant_job=consultant_job,
        consultant_data=consultant_data,
        selected_period=selected_period,
        competitors=competitors,
        competitor_summary=competitor_summary,
        customer_competitor_metrics=customer_competitor_metrics,
        competitor_job=competitor_job,
        competitor_history=competitor_history,
        competitor_config={"max_tracked": Config.COMPETITOR_MAX_TRACKED, "default_radius": Config.COMPETITOR_SEARCH_RADIUS_METERS,
                           "min_reviews": Config.COMPETITOR_MIN_REVIEW_COUNT,
                           "source_ready": bool(google_connection and google_connection.get("google_place_id") and google_connection.get("latitude") is not None and google_connection.get("longitude") is not None),
                           "api_configured": bool(Config.GOOGLE_PLACES_API_KEY)},
        minimum_review_message="Need at least 5 live Google reviews to generate reliable consultant insights.",
    )


@ai_consultant_bp.route(
    "/business/<int:business_id>/ai-consultant/generate",
    methods=["POST"],
)
@subscription_required
def generate_ai_consultant_report(business_id):
    if "user_id" not in session:
        return redirect("/login-page")

    if not user_owns_business(session["user_id"], business_id):
        return "Access denied", 403

    google_connection = _load_google_connection(business_id)
    google_location_id = google_connection.get("google_location_id") if google_connection else None
    metrics = get_business_review_metrics(
        business_id,
        source="google",
        google_location_id=google_location_id,
        require_google_review_id=True,
    )
    if metrics["total_reviews"] < 5:
        flash("Need at least 5 live Google reviews to generate reliable consultant insights.", "warning")
        return redirect(f"/business/{business_id}/ai-consultant")

    job_id, created = create_consultant_job(session["user_id"], business_id)
    flash(
        "AI Business Consultant generation queued."
        if created else "AI Business Consultant generation is already in progress.",
        "success" if created else "info",
    )
    requested_period = request.form.get("period")
    if requested_period is None:
        return redirect(f"/business/{business_id}/ai-consultant?job={job_id}")
    selected_period = resolve_period(requested_period)["days"]
    return redirect(f"/business/{business_id}/ai-consultant?period={selected_period}&job={job_id}")


@ai_consultant_bp.route(
    "/business/<int:business_id>/ai-consultant/actions/<int:action_id>/status",
    methods=["POST"],
)
@subscription_required
def update_ai_consultant_action_status(business_id, action_id):
    if "user_id" not in session:
        return redirect("/login-page")

    if not user_owns_business(session["user_id"], business_id):
        return "Access denied", 403

    status = request.form.get("status")
    owner_note = request.form.get("owner_note") or None

    try:
        updated = update_consultant_action_status(
            action_id,
            business_id,
            status,
            owner_note=owner_note,
        )
        if updated:
            flash("Consultant action updated.", "success")
        else:
            flash("Consultant action was not found.", "warning")
    except ValueError as error:
        flash(str(error), "warning")
    except Exception:
        current_app.logger.exception(
            "Failed to update consultant action status: business_id=%s action_id=%s",
            business_id,
            action_id,
        )
        flash("Could not update the consultant action. Please try again.", "danger")

    requested_period = request.form.get("period")
    if requested_period is None:
        return redirect(f"/business/{business_id}/ai-consultant")
    selected_period = resolve_period(requested_period)["days"]
    return redirect(f"/business/{business_id}/ai-consultant?period={selected_period}")


@ai_consultant_bp.route("/business/<int:business_id>/ai-consultant/competitors/search")
@subscription_required
def search_competitors(business_id):
    guard = _competitor_guard(business_id)
    if guard: return guard
    if not allow_competitor_search(session["user_id"], business_id):
        return jsonify({"message": "Too many competitor searches. Please try again later."}), 429
    business = _load_business(business_id); connection = _load_google_connection(business_id)
    source = _source_business(business, connection)
    try:
        result = discover_competitors(source, radius_meters=request.args.get("radius"), query=request.args.get("q") or request.args.get("category"))
        tracked = {item["google_place_id"] for item in list_competitors(business_id)}
        for candidate in result["candidates"]: candidate["tracked"] = candidate["google_place_id"] in tracked
        return jsonify(result)
    except (PlacesError, ValueError) as error:
        return jsonify({"message": str(error)}), 400


@ai_consultant_bp.route("/business/<int:business_id>/ai-consultant/competitors", methods=["POST"])
@subscription_required
def add_competitors(business_id):
    guard = _competitor_guard(business_id)
    if guard: return guard
    business = _load_business(business_id); connection = _load_google_connection(business_id); source = _source_business(business, connection)
    place_ids = list(dict.fromkeys(request.form.getlist("place_id")))[:Config.COMPETITOR_MAX_TRACKED]
    if not place_ids: flash("Select at least one competitor.", "warning"); return _competitor_redirect(business_id)
    added = 0
    try:
        for place_id in place_ids:
            details = get_place_details(validate_place_id(place_id), source=source)
            if details["google_place_id"] == source.get("google_place_id"): continue
            add_competitor(business_id, details); added += 1
        flash(f"Added {added} competitor{'s' if added != 1 else ''}.", "success")
    except (PlacesError, ValueError) as error: flash(str(error), "warning")
    return _competitor_redirect(business_id)


@ai_consultant_bp.route("/business/<int:business_id>/ai-consultant/competitors/<int:competitor_id>/remove", methods=["POST"])
@subscription_required
def remove_tracked_competitor(business_id, competitor_id):
    guard = _competitor_guard(business_id)
    if guard: return guard
    flash("Competitor removed." if remove_competitor(business_id, competitor_id) else "Competitor was not found.", "success")
    return _competitor_redirect(business_id)


@ai_consultant_bp.route("/business/<int:business_id>/ai-consultant/competitors/<int:competitor_id>/refresh", methods=["POST"])
@subscription_required
def refresh_tracked_competitor(business_id, competitor_id):
    guard = _competitor_guard(business_id)
    if guard: return guard
    competitor = get_competitor(business_id, competitor_id)
    if not competitor: flash("Competitor was not found.", "warning"); return _competitor_redirect(business_id)
    if not refresh_allowed(competitor) and session.get("role") != "admin":
        flash("Competitor data was refreshed recently. Please try again later.", "info"); return _competitor_redirect(business_id)
    try:
        source = _source_business(_load_business(business_id), _load_google_connection(business_id))
        update_competitor(business_id, competitor_id, get_place_details(competitor["google_place_id"], source=source))
        flash("Competitor details refreshed.", "success")
    except PlacesError as error: flash(str(error), "warning")
    return _competitor_redirect(business_id)


@ai_consultant_bp.route("/business/<int:business_id>/ai-consultant/competitors/refresh", methods=["POST"])
@subscription_required
def refresh_all_competitors(business_id):
    guard = _competitor_guard(business_id)
    if guard: return guard
    try:
        job_id, created = create_competitor_refresh_job(session["user_id"], business_id)
        flash("Competitor refresh queued." if created else "A competitor refresh is already active or was completed recently.", "success" if created else "info")
    except ValueError as error:
        flash(str(error), "warning")
        return _competitor_redirect(business_id)
    period = resolve_period(request.form.get("period"))["days"]
    return redirect(f"/business/{business_id}/ai-consultant?period={period}&competitor_job={job_id}#competitors")


def _load_business(business_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT id, business_name, business_type, city, state, country
            FROM businesses
            WHERE id=%s
            """,
            (business_id,)
        )
        return cursor.fetchone()
    finally:
        cursor.close()
        conn.close()


def _load_google_connection(business_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT
                id,
                is_connected,
                google_location_id,
                google_location_name,
                COALESCE(google_email, google_account_email) AS google_email,
                connected_at,
                google_place_id, latitude, longitude, primary_category, formatted_address,
                last_sync_at
            FROM google_business_connections
            WHERE business_id=%s
            AND is_connected=TRUE
            ORDER BY connected_at DESC, updated_at DESC
            LIMIT 1
            """,
            (business_id,)
        )
        return cursor.fetchone()
    finally:
        cursor.close()
        conn.close()


def _source_business(business, connection):
    connection = connection or {}
    return {"business_id": business["id"], "name": business["business_name"], "business_type": business.get("business_type"),
            "google_place_id": connection.get("google_place_id"), "latitude": connection.get("latitude"),
            "longitude": connection.get("longitude"), "primary_category": connection.get("primary_category") or business.get("business_type")}


def _competitor_guard(business_id):
    if "user_id" not in session: return redirect("/login-page")
    if not user_owns_business(session["user_id"], business_id): return ("Access denied", 403)
    return None


def _competitor_redirect(business_id):
    period = resolve_period(request.form.get("period"))["days"]
    return redirect(f"/business/{business_id}/ai-consultant?period={period}#competitors")
