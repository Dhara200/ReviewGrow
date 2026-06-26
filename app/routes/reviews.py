import os
import pandas as pd
from flask import Blueprint, request, jsonify, redirect, session, render_template
from app.services.database_service import (
    get_connection,
    user_owns_business
)
from app.services.gemini_service import analyze_reviews
import json

review_bp = Blueprint("reviews", __name__)


@review_bp.route("/reviews", methods=["POST"])
def create_review():
    if "user_id" not in session:
        return jsonify({
            "message": "Login required"
        }), 401
  
        

    if not user_owns_business(
        session["user_id"],
        business_id
    ):
        return jsonify({
            "message": "Access denied"
        }), 403

    try:
        data = request.get_json()

        business_id = data.get("business_id")
        source = data.get("source")
        rating = data.get("rating")
        review_text = data.get("review_text")
        reviewer_name = data.get("reviewer_name")
        review_date = data.get("review_date")

        conn = get_connection()

        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO reviews
            (
                business_id,
                source,
                rating,
                review_text,
                reviewer_name,
                review_date
            )
            VALUES
            (%s,%s,%s,%s,%s,%s)
            """,
            (
                business_id,
                source,
                rating,
                review_text,
                reviewer_name,
                review_date
            )
        )

        conn.commit()

        review_id = cursor.lastrowid

        cursor.close()
        conn.close()

        return jsonify({
            "message": "Review created successfully",
            "review_id": review_id
        }), 201

    except Exception as e:
        return jsonify({
            "message": str(e)
        }), 500


@review_bp.route("/reviews/upload", methods=["POST"])
def upload_reviews():
    if "user_id" not in session:
        return redirect("/login-page")

    if not user_owns_business(
        session["user_id"],
        business_id
    ):
        return "Access denied", 403

    try:
        business_id = request.form.get("business_id")

        if not business_id:
            return jsonify({
                "message": "business_id is required"
            }), 400

        if "file" not in request.files:
            return jsonify({
                "message": "No file uploaded"
            }), 400

        file = request.files["file"]

        if file.filename == "":
            return jsonify({
                "message": "No file selected"
            }), 400

        upload_path = os.path.join(
            "uploads",
            file.filename
        )

        file.save(upload_path)

        try:
            df = pd.read_csv(
                upload_path,
                encoding="utf-8"
            )
        except UnicodeDecodeError:
            df = pd.read_csv(
                upload_path,
                encoding="latin1"
            )

        conn = get_connection()
        cursor = conn.cursor()

        inserted_count = 0
        print(df.columns.tolist())
        for _, row in df.iterrows():
            review_date = pd.to_datetime(
                row.get("review_date"),
                dayfirst=True,
                errors="coerce"
            )

            if pd.isna(review_date):
                review_date = None
            else:
                review_date = review_date.strftime("%Y-%m-%d")
            rating = row.get("rating")

            if pd.isna(rating):
               rating = None
            else:
               rating = float(rating)
               
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
                    analysis_status
                )
                VALUES
                (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    business_id,
                    row.get("source") or "csv",
                    rating,
                    row.get("review_title") or "",
                    row.get("review_text") or "",
                    row.get("reviewer_name") or "Anonymous",
                    review_date,
                    "pending"
                )
            )

            inserted_count += 1

        conn.commit()

        cursor.close()
        conn.close()

        return jsonify({
            "message": "Reviews uploaded successfully",
            "reviews_inserted": inserted_count
        })

    except Exception as e:
        return jsonify({
            "message": str(e)
        }), 500


@review_bp.route("/reviews/upload-ui", methods=["POST"])
def upload_reviews_ui():
    try:
        business_id = request.form.get("business_id")

        if not business_id:
            return "Business ID missing"

        if "file" not in request.files:
            return "No file uploaded"

        file = request.files["file"]

        if file.filename == "":
            return "No file selected"

        upload_path = os.path.join(
            "uploads",
            file.filename
        )

        file.save(upload_path)

        df = pd.read_csv(upload_path)

        conn = get_connection()
        cursor = conn.cursor()

        for _, row in df.iterrows():
            review_date = pd.to_datetime(
                row.get("review_date"),
                dayfirst=True,
                errors="coerce"
            )

            if pd.isna(review_date):
                review_date = None
            else:
                review_date = review_date.strftime("%Y-%m-%d")

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
                    analysis_status
                )
                VALUES
                (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    business_id,
                    row.get("source") or "csv",
                    rating,
                    row.get("review_title") or "",
                    row.get("review_text") or "",
                    row.get("reviewer_name") or "Anonymous",
                    review_date,
                    "pending"
                )
            )

        conn.commit()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
"""
SELECT review_text
FROM reviews
WHERE business_id=%s
AND analysis_status='pending'
""",
(business_id,)
)

        reviews = cursor.fetchall()
        review_texts=[]

        for review in reviews:
         review_texts.append(
         review["review_text"]
    )

        result=analyze_reviews(review_texts)

        cursor.execute(
"""
INSERT INTO reports
(
business_id,
summary,
top_complaints,
top_praises,
recommendations,
sentiment_score,
review_count
)
VALUES
(%s,%s,%s,%s,%s,%s,%s)
""",
(
           business_id,
result["summary"],
json.dumps(result["top_complaints"]),
json.dumps(result["top_praises"]),
json.dumps(result["recommendations"]),
result["sentiment_score"],
len(reviews)
)
)

        cursor.execute(
"""
UPDATE reviews
SET analysis_status='analyzed'
WHERE business_id=%s
AND analysis_status='pending'
""",
(business_id,)
)

        conn.commit()
        cursor.close()
        conn.close()

        return redirect(
            f"/dashboard/{business_id}"
        )

    except Exception as e:
        return jsonify({
            "message": str(e)
        }), 500


@review_bp.route("/reviews/<int:business_id>", methods=["GET"])
def get_reviews(business_id):
    if "user_id" not in session:
        return jsonify({
            "message": "Login required"
        }), 401

    if not user_owns_business(
        session["user_id"],
        business_id
    ):
        return jsonify({
            "message": "Access denied"
        }), 403

    try:
        conn = get_connection()

        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            """
            SELECT
                id,
                source,
                rating,
                review_title,
                review_text,
                reviewer_name,
                review_date,
                analysis_status
            FROM reviews
            WHERE business_id=%s
            ORDER BY created_at DESC
            """,
            (business_id,)
        )

        reviews = cursor.fetchall()

        cursor.close()
        conn.close()

        return jsonify(reviews)

    except Exception as e:
        return jsonify({
            "message": str(e)
        }), 500 
         
@review_bp.route("/reviews/history/<int:business_id>")
def review_history(business_id):

    # login check
    ...

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
            analysis_status
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
        


        rating = str(int(review["rating"])) if review["rating"] else "0"

        if rating in rating_counts:
            rating_counts[rating] += 1
            
        source = (review["source"] or "Unknown").strip()
        source_counts[source] = source_counts.get(source, 0) + 1
        
        if review["analysis_status"] == "pending":
            pending_count += 1

        elif review["analysis_status"] == "analyzed":
            analyzed_count += 1

    cursor.close()
    conn.close()

    return render_template(
        "review_history.html",
        reviews=reviews,
        business_id=business_id,
        rating_counts=rating_counts,
        pending_count=pending_count,
        source_counts=source_counts,
        analyzed_count=analyzed_count
    )