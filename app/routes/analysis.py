from flask import Blueprint, current_app, jsonify, make_response, request
from flask import redirect
from flask import render_template
from flask import session
from app.services.ai_service import AIService, log_ai_usage
from app.services.analysis_job_service import (
    create_analysis_job,
    get_job_status_for_user
)
from app.services.database_service import get_connection, user_owns_business
from app.services.subscription_service import subscription_required
from app.routes.auth import get_client_ip
from app.services.sync_ai_security_service import (
    AISecurityUnavailable,
    AIQuotaExceeded,
    AIRequestInProgress,
    acquire_ai_quota_slot,
    consume_ai_rate_limits,
    validate_review_text,
)

analysis_bp = Blueprint("analysis", __name__)
ai_service = AIService()


def _assistant_error(message, status_code, retry_after=None):
    response = make_response(
        render_template("review_assistant.html", error_message=message),
        status_code,
    )
    if retry_after is not None:
        response.headers["Retry-After"] = str(max(1, int(retry_after)))
    return response


def _prepare_sync_ai_request():
    try:
        review_text = validate_review_text(request.form.get("review_text"))
    except ValueError as error:
        return None, _assistant_error(str(error), 400)

    try:
        scope, status = consume_ai_rate_limits(
            session["user_id"], get_client_ip()
        )
    except AISecurityUnavailable:
        current_app.logger.error(
            "Synchronous AI limiter unavailable: user_id=%s action=%s",
            session.get("user_id"),
            request.endpoint,
        )
        return None, _assistant_error(
            "AI is temporarily unavailable. Please try again later.", 503
        )
    if status is not None:
        retry_after = max(1, status.retry_after_seconds)
        current_app.logger.warning(
            "Synchronous AI rate limited: user_id=%s action=%s scope=%s "
            "retry_after=%s",
            session.get("user_id"),
            request.endpoint,
            scope,
            retry_after,
        )
        return None, _assistant_error(
            "Too many AI requests. Please wait before trying again.",
            429,
            retry_after,
        )

    try:
        slot = acquire_ai_quota_slot(
            session["user_id"],
            current_app.config["MAX_AI_REQUESTS_PER_MONTH"],
        )
    except AIRequestInProgress:
        return None, _assistant_error(
            "Another AI request is already in progress. Please wait.",
            429,
            1,
        )
    except AIQuotaExceeded:
        return None, _assistant_error(
            "Your monthly AI request quota has been reached.", 429
        )
    except AISecurityUnavailable:
        current_app.logger.error(
            "Synchronous AI quota unavailable: user_id=%s action=%s",
            session.get("user_id"),
            request.endpoint,
        )
        return None, _assistant_error(
            "AI is temporarily unavailable. Please try again later.", 503
        )
    return (review_text, slot), None


def _run_sync_ai(prompt, operation_type):
    prepared, error_response = _prepare_sync_ai_request()
    if error_response is not None:
        return None, None, error_response
    review_text, slot = prepared
    try:
        result = ai_service.generate_json(
            prompt(review_text), operation_type
        )
        log_ai_usage(
            slot.cursor, session["user_id"], None, result
        )
        slot.connection.commit()
        return result, review_text, None
    except Exception:
        try:
            slot.connection.rollback()
        except Exception:
            pass
        current_app.logger.error(
            "Synchronous AI provider failure: user_id=%s action=%s",
            session.get("user_id"),
            operation_type,
        )
        return None, None, _assistant_error(
            "AI processing is temporarily unavailable. Please try again later.",
            503,
        )
    finally:
        slot.close()

#ANALYSIS PAGE ROUTE

@analysis_bp.route("/review-assistant")
@subscription_required
def review_assistant():

  return render_template(
    "review_assistant.html"
)
    
@analysis_bp.route(
"/review-assistant/analyze",
methods=["POST"]
)
@subscription_required
def analyze_single_review():
    def prompt(review_text):
        return f"""
```

Analyze this review.

Return ONLY valid JSON.

{{
"sentiment":"",
"positives":[""],
"issues":[""],
"summary":""
}}

Review:

{review_text}
"""
    result, _, error_response = _run_sync_ai(
        prompt, "review_assistant_analysis"
    )
    if error_response is not None:
        return error_response
    data = result.data

    return render_template(
        "review_assistant.html",
        analysis_result=True,
        sentiment=data["sentiment"],
        positives=data["positives"],
        issues=data["issues"],
        summary=data["summary"]
    )

#  review-assistant REPLY PAGE ROUTE
      
@analysis_bp.route(
"/review-assistant/reply",
methods=["POST"]
)
@subscription_required

def generate_review_reply():
    def prompt(review_text):
        return f"""

Analyze this customer review and generate a professional reply.

Return ONLY valid JSON.

{{
"sentiment":"",
"reply":""
}}

Review:

{review_text}
"""
    result, _, error_response = _run_sync_ai(prompt, "review_reply")
    if error_response is not None:
        return error_response
    result_json = result.data

    return render_template(
        "review_assistant.html",
        result=True,
        sentiment=result_json["sentiment"],
        reply=result_json["reply"]
    )

@analysis_bp.route("/businesses/<int:business_id>/analysis-jobs", methods=["POST"])
@subscription_required
def create_business_analysis_job(business_id):
    if "user_id" not in session:
        return jsonify({"message": "Login required"}), 401

    if not user_owns_business(session["user_id"], business_id):
        return jsonify({"message": "Access denied"}), 403

    force_reanalysis = request.form.get("force_reanalysis") == "1"
    if request.is_json:
        force_reanalysis = bool((request.get_json(silent=True) or {}).get("force_reanalysis"))

    job_id, created = create_analysis_job(
        session["user_id"],
        business_id,
        force_reanalysis=force_reanalysis
    )

    return jsonify({
        "success": True,
        "job_id": job_id,
        "created": created,
        "status": "pending" if created else "already_running",
        "status_url": f"/analysis-jobs/{job_id}/status"
    }), 201 if created else 200


@analysis_bp.route("/analysis-jobs/<int:job_id>/status", methods=["GET"])
@subscription_required
def analysis_job_status(job_id):
    if "user_id" not in session:
        return jsonify({"message": "Login required"}), 401

    job = get_job_status_for_user(
        job_id,
        session["user_id"],
        is_admin=session.get("role") == "admin"
    )

    if not job:
        return jsonify({"message": "Job not found"}), 404

    return jsonify(job)


@analysis_bp.route("/analysis-jobs/<int:job_id>/retry", methods=["POST"])
@subscription_required
def retry_analysis_job(job_id):
    if "user_id" not in session:
        return jsonify({"message": "Login required"}), 401

    job = get_job_status_for_user(
        job_id,
        session["user_id"],
        is_admin=session.get("role") == "admin"
    )

    if not job:
        return jsonify({"message": "Job not found"}), 404

    if job["status"] not in ("failed", "completed"):
        return jsonify({"message": "Only failed or completed jobs can be retried."}), 400

    job_id, created = create_analysis_job(
        session["user_id"],
        job["business_id"],
        force_reanalysis=True
    )

    return jsonify({
        "success": True,
        "job_id": job_id,
        "created": created,
        "status_url": f"/analysis-jobs/{job_id}/status"
    })
        
 
