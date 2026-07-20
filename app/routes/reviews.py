import os
import math
from urllib.parse import urlencode
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
DEFAULT_REVIEW_PAGE_SIZE = 25
ALLOWED_REVIEW_PAGE_SIZES = {25, 50, 100}


def _positive_int_arg(name, default):
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(value, 1)


def _review_page_size():
    try:
        value = int(request.args.get("per_page", DEFAULT_REVIEW_PAGE_SIZE))
    except (TypeError, ValueError):
        return DEFAULT_REVIEW_PAGE_SIZE
    return value if value in ALLOWED_REVIEW_PAGE_SIZES else DEFAULT_REVIEW_PAGE_SIZE


def _query_url(path, **updates):
    values = request.args.to_dict(flat=True)
    for key, value in updates.items():
        if value in (None, ""):
            values.pop(key, None)
        else:
            values[key] = str(value)
    return f"{path}?{urlencode(values)}" if values else path


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
                    "excel",
                    review["rating"],
                    review["review_title"],
                    review["review_text"],
                    review["reviewer_name"],
                    review["review_date"],
                    "excel",
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

    page = _positive_int_arg("page", 1)
    per_page = _review_page_size()
    filters = {
        "search": (request.args.get("search") or "").strip(),
        "status": request.args.get("status") or "",
        "rating": request.args.get("rating") or "",
        "source": request.args.get("source") or "",
        "sentiment": request.args.get("sentiment") or "",
        "reply_status": request.args.get("reply_status") or "",
        "date_from": request.args.get("date_from") or "",
        "date_to": request.args.get("date_to") or "",
    }
    clauses = ["business_id=%s"]
    params = [business_id]

    if filters["search"]:
        clauses.append("(review_text LIKE %s OR reviewer_name LIKE %s OR source LIKE %s)")
        like = f"%{filters['search']}%"
        params.extend([like, like, like])
    if filters["status"] in {"pending", "analyzed", "failed"}:
        clauses.append("analysis_status=%s")
        params.append(filters["status"])
    if filters["rating"] in {"1", "2", "3", "4", "5"}:
        clauses.append("ROUND(COALESCE(review_rating, rating))=%s")
        params.append(int(filters["rating"]))
    if filters["source"]:
        clauses.append("source=%s")
        params.append(filters["source"])
    if filters["sentiment"].lower() in {"positive", "neutral", "negative"}:
        clauses.append("LOWER(COALESCE(sentiment, ''))=%s")
        params.append(filters["sentiment"].lower())
    if filters["reply_status"] in {"pending", "approved", "posted", "failed"}:
        clauses.append("reply_status=%s")
        params.append(filters["reply_status"])
    if filters["date_from"]:
        clauses.append("COALESCE(review_created_at, review_date, created_at) >= %s")
        params.append(filters["date_from"])
    if filters["date_to"]:
        clauses.append("COALESCE(review_created_at, review_date, created_at) < DATE_ADD(%s, INTERVAL 1 DAY)")
        params.append(filters["date_to"])

    where_sql = " AND ".join(clauses)
    cursor.execute(
        f"SELECT COUNT(*) AS total_count FROM reviews WHERE {where_sql}",
        tuple(params)
    )
    total_count = int(cursor.fetchone()["total_count"] or 0)
    total_pages = math.ceil(total_count / per_page) if total_count else 0
    offset = (page - 1) * per_page

    cursor.execute(f"""
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
        WHERE {where_sql}
        ORDER BY COALESCE(review_updated_at, review_created_at, review_date, created_at) DESC,
                 id DESC
        LIMIT %s OFFSET %s
    """, tuple([*params, per_page, offset]))

    reviews = cursor.fetchall()

    cursor.execute(
        """
        SELECT COUNT(*) AS total_count,
            SUM(analysis_status='pending') AS pending_count,
            SUM(analysis_status='analyzed') AS analyzed_count,
            SUM(ROUND(COALESCE(review_rating, rating))=5) AS rating_5,
            SUM(ROUND(COALESCE(review_rating, rating))=4) AS rating_4,
            SUM(ROUND(COALESCE(review_rating, rating))=3) AS rating_3,
            SUM(ROUND(COALESCE(review_rating, rating))=2) AS rating_2,
            SUM(ROUND(COALESCE(review_rating, rating))=1) AS rating_1
        FROM reviews WHERE business_id=%s
        """,
        (business_id,)
    )
    counts = cursor.fetchone() or {}
    rating_counts = {str(value): int(counts.get(f"rating_{value}") or 0) for value in range(1, 6)}
    cursor.execute(
        """
        SELECT COALESCE(source, 'Unknown') AS source, COUNT(*) AS source_count
        FROM reviews WHERE business_id=%s
        GROUP BY COALESCE(source, 'Unknown') ORDER BY source ASC
        """,
        (business_id,)
    )
    source_counts = {row["source"]: int(row["source_count"] or 0) for row in cursor.fetchall()}
    pending_count = int(counts.get("pending_count") or 0)
    analyzed_count = int(counts.get("analyzed_count") or 0)
    all_review_count = int(counts.get("total_count") or 0)

    pagination = {
        "page": page, "per_page": per_page, "total": total_count,
        "total_pages": total_pages,
        "start": offset + 1 if reviews else 0,
        "end": offset + len(reviews),
        "previous_url": _query_url(request.path, page=page - 1) if page > 1 else None,
        "next_url": _query_url(request.path, page=page + 1) if page < total_pages else None,
        "page_size_urls": {size: _query_url(request.path, page=1, per_page=size) for size in sorted(ALLOWED_REVIEW_PAGE_SIZES)},
    }
    filter_urls = {
        "all": _query_url(request.path, page=1, status=None, rating=None, source=None),
        "status": {value: _query_url(request.path, page=1, status=value) for value in ("pending", "analyzed")},
        "rating": {str(value): _query_url(request.path, page=1, rating=value) for value in range(1, 6)},
        "source": {value: _query_url(request.path, page=1, source=value) for value in source_counts},
    }

    cursor.close()
    conn.close()

    return render_template(
        "review_history.html",
        business_id=business_id,
        reviews=reviews,
        filters=filters,
        pagination=pagination,
        filter_urls=filter_urls,
        all_review_count=all_review_count,
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
