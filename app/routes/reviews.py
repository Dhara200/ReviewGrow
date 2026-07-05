import os
from uuid import uuid4
from flask import Blueprint, flash, request, jsonify, redirect, session, render_template
from werkzeug.utils import secure_filename
from app.services.database_service import (
    get_connection,
    user_owns_business
)
from app.services.analysis_job_service import create_analysis_job
from app.utils.csv_parser import normalize_csv_row as normalize_review_row
from app.utils.excel_parser import read_reviews_file
from app.services.subscription_service import subscription_required


review_bp = Blueprint("reviews", __name__)

ALLOWED_REVIEW_EXTENSIONS = {".xlsx", ".xls"}


def _safe_upload_path(filename):
    safe_name = secure_filename(filename)
    extension = os.path.splitext(safe_name)[1].lower()

    if extension not in ALLOWED_REVIEW_EXTENSIONS:
        raise ValueError("Unsupported file type. Please upload an Excel file.")

    os.makedirs("uploads", exist_ok=True)

    return os.path.join(
        "uploads",
        f"{uuid4().hex}{extension}"
    )

# ==========================================
# REVIEW UPLOAD
# ==========================================

@review_bp.route("/reviews/upload-ui", methods=["POST"])
@subscription_required
def upload_reviews_ui():

    if "user_id" not in session:
        return redirect("/login-page")

    business_id = request.form.get("business_id")
    upload_redirect = f"/upload-reviews/{business_id}" if business_id else "/my-businesses"

    try:

        if not business_id:
            flash("Business ID missing. Please choose a business and try again.", "danger")
            return redirect("/my-businesses")

        if not user_owns_business(session["user_id"], business_id):
            return "Access denied", 403

        if "file" not in request.files:
            flash("Please choose an Excel file before uploading.", "danger")
            return redirect(upload_redirect)

        file = request.files["file"]

        if file.filename == "":
            flash("Please choose an Excel file before uploading.", "danger")
            return redirect(upload_redirect)

        upload_path = _safe_upload_path(file.filename)
        file.save(upload_path)

        df = read_reviews_file(upload_path)

        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        inserted_count = 0
        uploaded_review_texts = []

        for _, row in df.iterrows():

            review = normalize_review_row(row)

            if not review["review_text"]:
                continue

            cursor.execute(
                """
                INSERT INTO reviews
                (
                    business_id,
                    source,
                    rating,
                    review_title,
                    review_text,
                    reviewer_name,
                    review_date,
                    source_platform,
                    analysis_status
                )
                VALUES
                (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    business_id,
                    review["source"],
                    review["rating"],
                    review["review_title"],
                    review["review_text"],
                    review["reviewer_name"],
                    review["review_date"],
                    review["source"],
                    "pending"
                )
            )

            inserted_count += 1
            uploaded_review_texts.append(review["review_text"])

        if inserted_count == 0:

            cursor.close()
            conn.close()

            flash("No valid reviews were found in the Excel file.", "warning")
            return redirect(upload_redirect)
        conn.commit()

        cursor.close()
        conn.close()

        job_id, created = create_analysis_job(
            session["user_id"],
            business_id,
            force_reanalysis=False
        )
        if created:
            flash(
                f"Uploaded {inserted_count} reviews. AI analysis job #{job_id} is queued.",
                "success"
            )
        else:
            flash(
                f"Uploaded {inserted_count} reviews. Existing analysis job #{job_id} is still running.",
                "info"
            )

        return redirect(
            f"/dashboard/{business_id}?job={job_id}"
        )

        

    except ValueError as e:
        flash(str(e), "danger")
        return redirect(upload_redirect)

    except Exception:

        flash("Review upload failed. Please check the Excel file and try again.", "danger")
        return redirect(upload_redirect)

# ==========================================
# REVIEW HISTORY
# ==========================================

@review_bp.route("/reviews/history/<int:business_id>")
@subscription_required
def review_history(business_id):

    if "user_id" not in session:
        return redirect("/login-page")

    if not user_owns_business(session["user_id"], business_id):
        return "Access denied", 403

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT
            id,
            source,
            rating,
            review_title,
            review_text,
            reviewer_name,
            review_date,
            analysis_status,
            sentiment,
            summary,
            ai_reply,
            analyzed_at
        FROM reviews
        WHERE business_id=%s
        ORDER BY review_date DESC
    """, (business_id,))

    reviews = cursor.fetchall()

    rating_counts = {
        "5": 0,
        "4": 0,
        "3": 0,
        "2": 0,
        "1": 0
    }

    source_counts = {}

    pending_count = 0
    analyzed_count = 0

    for review in reviews:

        if review["rating"] is not None:

            rating = str(int(review["rating"]))

            if rating in rating_counts:
                rating_counts[rating] += 1

        source = review["source"] or "Unknown"
        source_counts[source] = source_counts.get(source, 0) + 1

        if review["analysis_status"] == "pending":
            pending_count += 1

        elif review["analysis_status"] == "analyzed":
            analyzed_count += 1

    cursor.close()
    conn.close()

    return render_template(
        "review_history.html",
        business_id=business_id,
        reviews=reviews,
        rating_counts=rating_counts,
        source_counts=source_counts,
        pending_count=pending_count,
        analyzed_count=analyzed_count
    )
    
# ==========================================
# SINGLE REVIEW ANALYSIS
# ==========================================  
 
@review_bp.route("/reviews/analyze/<int:review_id>", methods=["POST"])
@subscription_required
def analyze_single_review_route(review_id):

    if "user_id" not in session:
        return jsonify({
            "message": "Login required"
        }), 401

    try:

        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        # Make sure the review belongs to the logged-in user
        if session.get("role") == "admin":
            cursor.execute(
                """
                SELECT
                    r.id,
                    r.business_id,
                    r.review_text,
                    r.analysis_status
                FROM reviews r
                WHERE r.id = %s
                """,
                (review_id,)
            )
        else:
            cursor.execute(
                """
                SELECT
                    r.id,
                    r.business_id,
                    r.review_text,
                    r.analysis_status
                FROM reviews r
                JOIN businesses b
                    ON r.business_id = b.id
                WHERE
                    r.id = %s
                    AND b.user_id = %s
                """,
                (
                    review_id,
                    session["user_id"]
                )
            )

        review = cursor.fetchone()

        if not review:
            cursor.close()
            conn.close()

            return jsonify({
                "message": "Review not found"
            }), 404

        if not review["review_text"] or review["review_text"].strip() == "":
            cursor.close()
            conn.close()

            return jsonify({
                "message": "Review text is empty."
            }), 400

        if review["analysis_status"] == "analyzed":
            cursor.execute(
                """
                SELECT sentiment, summary, ai_reply
                FROM reviews
                WHERE id=%s
                """,
                (review_id,)
            )
            existing = cursor.fetchone()

            cursor.close()
            conn.close()

            return jsonify({
                "success": True,
                "sentiment": existing["sentiment"],
                "summary": existing["summary"],
                "reply": existing["ai_reply"]
            })

        cursor.close()
        conn.close()

        job_id, created = create_analysis_job(
            session["user_id"],
            review["business_id"],
            force_reanalysis=False
        )

        return jsonify({

            "success": True,
            "queued": True,
            "job_id": job_id,
            "created": created,
            "status_url": f"/analysis-jobs/{job_id}/status"

        })

    except Exception as e:

        return jsonify({
            "message": str(e)
        }), 500
        
# ==========================================
# VIEW SINGLE REVIEW ANALYSIS
# ==========================================

@review_bp.route("/reviews/analysis/<int:review_id>")
@subscription_required
def get_review_analysis(review_id):

    if "user_id" not in session:
        return jsonify({
            "message": "Login required"
        }), 401

    try:

        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        if session.get("role") == "admin":
            cursor.execute(
                """
                SELECT
                    sentiment,
                    summary,
                    ai_reply
                FROM reviews
                WHERE id=%s
                """,
                (review_id,)
            )
        else:
            cursor.execute(
                """
                SELECT
                    r.sentiment,
                    r.summary,
                    r.ai_reply,
                    b.user_id
                FROM reviews r
                JOIN businesses b
                    ON r.business_id = b.id
                WHERE
                    r.id=%s
                    AND b.user_id=%s
                """,
                (
                    review_id,
                    session["user_id"]
                )
            )

        review = cursor.fetchone()

        cursor.close()
        conn.close()

        if not review:

            return jsonify({
                "message": "Review not found"
            }), 404

        return jsonify({

            "sentiment":
                review["sentiment"],

            "summary":
                review["summary"],

            "ai_reply":
                review["ai_reply"]

        })

    except Exception as e:

        return jsonify({
            "message": str(e)
        }), 500
        
# ==========================================
# DELETE REVIEW
# ==========================================

@review_bp.route("/reviews/delete/<int:review_id>", methods=["POST"])
@subscription_required
def delete_review(review_id):

    if "user_id" not in session:
        return jsonify({
            "message": "Login required"
        }), 401

    try:

        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        # Verify ownership
        if session.get("role") == "admin":
            cursor.execute(
                """
                SELECT business_id
                FROM reviews
                WHERE id=%s
                """,
                (review_id,)
            )
        else:
            cursor.execute(
                """
                SELECT
                    r.business_id
                FROM reviews r
                JOIN businesses b
                    ON r.business_id = b.id
                WHERE
                    r.id=%s
                    AND b.user_id=%s
                """,
                (
                    review_id,
                    session["user_id"]
                )
            )

        review = cursor.fetchone()

        if not review:

            cursor.close()
            conn.close()

            return jsonify({
                "message": "Review not found"
            }), 404

        business_id = review["business_id"]

        cursor.execute(
            """
            DELETE FROM reviews
            WHERE id=%s
            """,
            (review_id,)
        )

        conn.commit()

        cursor.close()
        conn.close()

        return jsonify({

            "success": True,
            "business_id": business_id

        })

    except Exception as e:

        return jsonify({
            "message": str(e)
        }), 500
        
def save_review_analysis(cursor, review_id, analysis):

    cursor.execute(
        """
        UPDATE reviews
        SET
            sentiment=%s,
            summary=%s,
            ai_reply=%s,
            analysis_status='analyzed',
            analyzed_at=NOW()
        WHERE id=%s
        """,
        (
            analysis["sentiment"],
            analysis["summary"],
            analysis["ai_reply"],
            review_id
        )
    )
