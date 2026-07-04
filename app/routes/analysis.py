from flask import Blueprint, jsonify, request
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

analysis_bp = Blueprint("analysis", __name__)
ai_service = AIService()

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

  try:

    review_text = request.form.get(
        "review_text"
    )

    prompt = f"""
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

    result = ai_service.generate_json(prompt, "review_assistant_analysis")
    data = result.data

    if session.get("user_id"):
        conn = get_connection()
        cursor = conn.cursor()
        log_ai_usage(cursor, session["user_id"], None, result)
        conn.commit()
        cursor.close()
        conn.close()

    return render_template(
        "review_assistant.html",
        analysis_result=True,
        sentiment=data["sentiment"],
        positives=data["positives"],
        issues=data["issues"],
        summary=data["summary"]
    )

  except Exception as e:

    return {
        "message": str(e)
    }, 500

#  review-assistant REPLY PAGE ROUTE
      
@analysis_bp.route(
"/review-assistant/reply",
methods=["POST"]
)
@subscription_required

def generate_review_reply():
 try:

    review_text = request.form.get(
        "review_text"
    )

    prompt = f"""

Analyze this customer review and generate a professional reply.

Return ONLY valid JSON.

{{
"sentiment":"",
"reply":""
}}

Review:

{review_text}
"""
    result = ai_service.generate_json(prompt, "review_reply")
    result_json = result.data

    if session.get("user_id"):
        conn = get_connection()
        cursor = conn.cursor()
        log_ai_usage(cursor, session["user_id"], None, result)
        conn.commit()
        cursor.close()
        conn.close()

    return render_template(
        "review_assistant.html",
        result=True,
        sentiment=result_json["sentiment"],
        reply=result_json["reply"]
    )

 except Exception as e:

    return {
        "message": str(e)
    }, 500


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
        
 
