from flask import Blueprint, current_app, flash, redirect, render_template, request, session

from app.services.ai_consultant_service import (
    get_command_center_snapshot,
    get_latest_consultant_report,
)
from app.services.analysis_job_service import (
    create_consultant_job,
    get_active_job_for_business,
)
from app.services.business_analytics_service import (
    get_business_review_metrics,
)
from app.services.business_metrics_service import get_google_review_count_trend
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
    google_connected_at = google_connection.get("connected_at") if google_connection else None
    metrics = get_business_review_metrics(
        business_id,
        source="google",
        google_location_id=google_location_id,
        require_google_review_id=True,
    )
    report = get_latest_consultant_report(business_id)
    consultant_job = get_active_job_for_business(business_id, "ai_consultant")
    command_center = get_command_center_snapshot(
        business_id,
        report=report,
        google_location_id=google_location_id,
    )
    action_state = sync_consultant_actions(
        business_id,
        command_center,
        report_id=report.get("id") if report else None,
    )
    if command_center["health"].get("score") is not None:
        verified_bonus = min(action_state["analytics"]["verified"] * 0.2, 0.8)
        command_center["health"]["score"] = min(
            round(command_center["health"]["score"] + verified_bonus, 1),
            10,
        )
    command_center["alerts"] = filter_active_alerts(
        command_center["alerts"],
        action_state,
    )
    can_generate = metrics["total_reviews"] >= 5
    google_review_trend_data = get_google_review_count_trend(
        business_id,
        google_location_id=google_location_id,
        connected_at=google_connected_at,
    )

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
        google_review_trend_data=google_review_trend_data,
        consultant_job=consultant_job,
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
    return redirect(f"/business/{business_id}/ai-consultant?job={job_id}")


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

    return redirect(f"/business/{business_id}/ai-consultant")


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
