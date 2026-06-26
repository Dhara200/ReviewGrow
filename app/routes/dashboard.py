from flask import Blueprint, jsonify, request
import json
import io
from flask import render_template
from app.services.database_service import get_connection
from flask import send_file
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer
)
from reportlab.lib.styles import getSampleStyleSheet
from flask import session
from flask import redirect

from app.services.database_service import (
    get_connection,
    user_owns_business
)

from flask import session

dashboard_bp = Blueprint("dashboard", __name__)

@dashboard_bp.route("/report/<int:business_id>", methods=["GET"])
def get_report(business_id):

    try:

        conn = get_connection()

        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
SELECT review_text,
rating,
review_date
FROM reviews
WHERE business_id=%s
ORDER BY created_at DESC
LIMIT 10
""", (business_id,))

        reviews = cursor.fetchall()
        return render_template(
    "dashboard.html",
    report=report,
    reviews=reviews,
    business_id=business_id
)
        report = cursor.fetchone()
        
        if report:

            report["top_complaints"] = json.loads(
                report["top_complaints"]
            )

            report["top_praises"] = json.loads(
                report["top_praises"]
            )
            
        cursor.close()
        conn.close()

        if not report:

            return jsonify({
                "message": "No report found"
            }), 404

        return jsonify(report)

    except Exception as e:

        return jsonify({
            "message": str(e)
        }), 500
        
@dashboard_bp.route("/report/<int:business_id>/pdf", methods=["GET"])
def download_report_pdf(business_id):

    try:

        conn = get_connection()

        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            """
            SELECT
                b.business_name,
                r.summary,
                r.top_complaints,
                r.top_praises,
                r.sentiment_score,
                r.review_count,
                r.generated_at
            FROM reports r
            JOIN businesses b
                ON r.business_id = b.id
            WHERE r.business_id=%s
            ORDER BY r.generated_at DESC
            LIMIT 1
            """,
            (business_id,)
        )

        report = cursor.fetchone()

        cursor.close()
        conn.close()

        if not report:

            return {
                "message": "No report found"
            }, 404

        complaints = json.loads(
            report["top_complaints"]
        )

        praises = json.loads(
            report["top_praises"]
        )

        buffer = io.BytesIO()

        doc = SimpleDocTemplate(buffer)

        styles = getSampleStyleSheet()

        content = []

        content.append(
            Paragraph(
                "AI Reputation Report",
                styles["Title"]
            )
        )

        content.append(Spacer(1, 12))

        content.append(
            Paragraph(
                f"Business: {report['business_name']}",
                styles["Normal"]
            )
        )

        content.append(
            Paragraph(
                f"Sentiment Score: {report['sentiment_score']}%",
                styles["Normal"]
            )
        )

        content.append(
            Paragraph(
                f"Reviews Analyzed: {report['review_count']}",
                styles["Normal"]
            )
        )

        content.append(Spacer(1, 12))

        content.append(
            Paragraph(
                "Summary",
                styles["Heading2"]
            )
        )

        content.append(
            Paragraph(
                report["summary"],
                styles["Normal"]
            )
        )

        content.append(Spacer(1, 12))

        content.append(
            Paragraph(
                "Top Praises",
                styles["Heading2"]
            )
        )

        for item in praises:
            content.append(
                Paragraph(
                    f"• {item}",
                    styles["Normal"]
                )
            )

        content.append(Spacer(1, 12))

        content.append(
            Paragraph(
                "Top Complaints",
                styles["Heading2"]
            )
        )

        for item in complaints:
            content.append(
                Paragraph(
                    f"• {item}",
                    styles["Normal"]
                )
            )

        doc.build(content)

        buffer.seek(0)

        return send_file(
            buffer,
            as_attachment=True,
            download_name="reputation_report.pdf",
            mimetype="application/pdf"
        )

    except Exception as e:

        return {
            "message": str(e)
        }, 500

@dashboard_bp.route("/dashboard/<int:business_id>")
def dashboard(business_id):

    if "user_id" not in session:
        return redirect("/login-page")

    if not user_owns_business(
        session["user_id"],
        business_id
    ):
        return "Access denied", 403

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    report_id = request.args.get("report")

    if report_id:

        cursor.execute(
            """
            SELECT
                b.business_name,
                b.business_type,
                b.city,
                b.state,
                b.country,
                r.id,
                r.summary,
                r.top_complaints,
                r.top_praises,
                r.sentiment_score,
                r.review_count,
                r.generated_at,
                r.recommendations
            FROM reports r
            JOIN businesses b
                ON r.business_id = b.id
            WHERE r.id=%s
            AND r.business_id=%s
            AND b.user_id=%s
            """,
            (
                report_id,
                business_id,
                session["user_id"]
            )
        )

    else:

        cursor.execute(
            """
            SELECT
                b.business_name,
                b.business_type,
                b.city,
                b.state,
                b.country,
                r.id,
                r.summary,
                r.top_complaints,
                r.top_praises,
                r.sentiment_score,
                r.review_count,
                r.generated_at,
                r.recommendations
            FROM reports r
            JOIN businesses b
                ON r.business_id = b.id
            WHERE r.business_id=%s
            AND b.user_id=%s
            ORDER BY r.generated_at DESC
            LIMIT 1
            """,
            (
                business_id,
                session["user_id"]
            )
        )

    report = cursor.fetchone()

    cursor.execute(
        """
        SELECT
            id,
            sentiment_score,
            review_count,
            generated_at
        FROM reports
        WHERE business_id=%s
        ORDER BY generated_at DESC
        """,
        (business_id,)
    )

    report_history = cursor.fetchall()

    cursor.close()
    conn.close()

    if not report:
        return "No report found"

    report["top_complaints"] = json.loads(
        report["top_complaints"]
    )

    report["top_praises"] = json.loads(
        report["top_praises"]
    )

    if report.get("recommendations"):

        report["recommendations"] = json.loads(
            report["recommendations"]
        )

    else:

        report["recommendations"] = [
            "No recommendations available"
        ]

    for item in report_history:

        if item["generated_at"]:

            item["generated_at"] = item["generated_at"].strftime(
                "%d %b %Y %I:%M %p"
            )

    return render_template(
        "dashboard.html",
        report=report,
        report_history=report_history,
        business_id=business_id
    )

