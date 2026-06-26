from flask import Blueprint, jsonify, request
from app.services.database_service import get_connection
from app.services.gemini_service import analyze_reviews
import traceback
import json
from flask import redirect
from flask import render_template
from app.services.gemini_service import model

analysis_bp = Blueprint("analysis", __name__)


@analysis_bp.route("/review-assistant")
def review_assistant():

  return render_template(
    "review_assistant.html"
)



    
@analysis_bp.route(
"/review-assistant/analyze",
methods=["POST"]
)
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

    response = model.generate_content(
        prompt
    )

    result_text = (
        response.text
        .replace("```json","")
        .replace("```","")
        .strip()
    )

    data = json.loads(
        result_text
    )

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

        
@analysis_bp.route(
"/review-assistant/reply",
methods=["POST"]
)

def generate_review_reply():
 try:

    review_text = request.form.get(
        "review_text"
    )

    prompt = f"""
```

Analyze this review and generate a professional reply.

Return ONLY valid JSON.

{{
"sentiment":"",
"reply":""
}}

Review:

{review_text}
"""
    response = model.generate_content(
        prompt
    )

    result_text = response.text

    result_text = result_text.replace(
        "```json",
        ""
    )

    result_text = result_text.replace(
        "```",
        ""
    )

    result_json = json.loads(
        result_text.strip()
    )

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
        
@analysis_bp.route("/analyze", methods=["POST"])
def analyze():

    try:

        data = request.get_json()

        business_id = data.get("business_id")

        conn = get_connection()

        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            """
            SELECT *
            FROM reviews
            WHERE business_id=%s
            AND analysis_status='pending'
            """,
            (business_id,)
        )

        reviews = cursor.fetchall()

        if not reviews:

            return jsonify({
                "message": "No pending reviews found"
            })

        review_texts = []

        for review in reviews:
            review_texts.append(
                review["review_text"]
            )

        result = analyze_reviews(
            review_texts
        )
        print("========== AI RESULT ==========")
        print(result)
        print("===============================")
        print("Saving report...")
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
            (%s,%s,%s,%s,%s,%s)
            """,
            (
                business_id,
                result["summary"],
                json.dumps(result["top_complaints"]),
                json.dumps(result["top_praises"]),
                json.dumps(
    result.get(
        "recommendations",
        ["No recommendations generated"]
    )
),
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
        
        report_id = cursor.lastrowid

        conn.commit()

        cursor.close()
        conn.close()

        return jsonify({
            "message": "Analysis completed",
            "report_id": report_id
        })

    except Exception as e:

        if "429" in str(e):

            return {
            "message": "Daily AI quota reached. Please try again later or use a new API key."
        }, 429

    return {
        "message": str(e)
    }, 500
        
@analysis_bp.route("/analyze-ui", methods=["POST"])
def analyze_ui():

    try:

        business_id = request.form.get("business_id")

        # reuse existing logic

        conn = get_connection()

        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            """
            SELECT *
            FROM reviews
            WHERE business_id=%s
            AND analysis_status='pending'
            """,
            (business_id,)
        )

        reviews = cursor.fetchall()

        if not reviews:

            return redirect(
                f"/dashboard/{business_id}"
            )

        review_texts = []

        for review in reviews:
            review_texts.append(
                review["review_text"]
            )

        result = analyze_reviews(
            review_texts,
                print(result)
        )

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
            (%s,%s,%s,%s,%s,%s)
            """,
            (
                business_id,
                result["summary"],
                json.dumps(result["top_complaints"]),
                json.dumps(result["top_praises"]),
                json.dumps(
                  result.get(
                   "recommendations",
                  ["No recommendations generated"]
                )
            ),
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

        if "429" in str(e):

            return {
            "message": "Daily AI quota reached. Please try again later or use a new API key."
        }, 429

    return {
        "message": str(e)
    }, 500