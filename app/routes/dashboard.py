import io
import json
from xml.sax.saxutils import escape

from flask import Blueprint, jsonify, redirect, render_template, request, send_file, session
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.services.database_service import get_connection, user_owns_business
from app.services.subscription_service import subscription_required


dashboard_bp = Blueprint("dashboard", __name__)


def _json_list(value):
    if not value:
        return []

    if isinstance(value, list):
        return value

    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []

    return parsed if isinstance(parsed, list) else []


def _login_required_json():
    return jsonify({"message": "Login required"}), 401


def _can_access_business(business_id):
    if "user_id" not in session:
        return False

    if session.get("role") == "admin":
        return True

    return user_owns_business(session["user_id"], business_id)


def _business_health(score):
    score = score or 0

    if score >= 85:
        return "Excellent"
    if score >= 70:
        return "Good"
    if score >= 50:
        return "Average"
    return "Needs Improvement"


def _business_health_note(score):
    score = score or 0

    if score >= 85:
        return "Customer sentiment is very strong. Maintain service consistency and continue responding quickly to reviews."
    if score >= 70:
        return "Overall sentiment is healthy. Focus on repeated complaints to move from good to excellent."
    if score >= 50:
        return "Customer sentiment is mixed. Prioritize operational fixes and monitor review themes closely."
    return "Customer sentiment needs urgent attention. Address recurring complaints and improve service recovery workflows."


def _sentiment_estimates(review_count, sentiment_score):
    review_count = int(review_count or 0)
    score = float(sentiment_score or 0)

    positive = int(review_count * score / 100)
    negative = int(review_count * (100 - score) / 200)
    neutral = max(review_count - positive - negative, 0)

    return positive, neutral, negative


def _paragraph_text(value):
    return escape(str(value or ""))


def _safe_list(items, fallback):
    return items if items else [fallback]


@dashboard_bp.route("/report/<int:business_id>", methods=["GET"])
@subscription_required
def get_report(business_id):
    if "user_id" not in session:
        return _login_required_json()

    if not _can_access_business(business_id):
        return jsonify({"message": "Access denied"}), 403

    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            """
            SELECT *
            FROM reports
            WHERE business_id=%s
            ORDER BY generated_at DESC
            LIMIT 1
            """,
            (business_id,)
        )

        report = cursor.fetchone()

        cursor.close()
        conn.close()

        if not report:
            return jsonify({"message": "No report found"}), 404

        report["top_complaints"] = _json_list(report.get("top_complaints"))
        report["top_praises"] = _json_list(report.get("top_praises"))
        report["recommendations"] = _json_list(report.get("recommendations"))

        return jsonify(report)

    except Exception as e:
        return jsonify({"message": str(e)}), 500


@dashboard_bp.route("/report/<int:business_id>/pdf", methods=["GET"])
@subscription_required
def download_report_pdf(business_id):
    if "user_id" not in session:
        return redirect("/login-page")

    if not _can_access_business(business_id):
        return "Access denied", 403

    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            """
            SELECT
                b.business_name,
                b.business_type,
                b.city,
                b.state,
                b.country,
                r.summary,
                r.top_complaints,
                r.top_praises,
                r.recommendations,
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
            return {"message": "No report found"}, 404

        complaints = _json_list(report.get("top_complaints"))
        praises = _json_list(report.get("top_praises"))
        recommendations = _json_list(report.get("recommendations"))
        score = report.get("sentiment_score") or 0
        review_count = report.get("review_count") or 0
        business_health = _business_health(report.get("sentiment_score"))
        positive_count, neutral_count, negative_count = _sentiment_estimates(
            review_count,
            score
        )

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            rightMargin=42,
            leftMargin=42,
            topMargin=38,
            bottomMargin=34
        )
        styles = getSampleStyleSheet()

        title_style = ParagraphStyle(
            "TitleStyle",
            parent=styles["Title"],
            alignment=TA_LEFT,
            fontName="Helvetica-Bold",
            fontSize=22,
            leading=28,
            textColor=colors.HexColor("#0F172A"),
            spaceAfter=8
        )

        section_style = ParagraphStyle(
            "SectionStyle",
            parent=styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=13,
            leading=16,
            textColor=colors.HexColor("#2563EB"),
            spaceBefore=16,
            spaceAfter=8
        )

        normal_style = ParagraphStyle(
            "NormalStyle",
            parent=styles["Normal"],
            fontSize=9.5,
            leading=15,
            textColor=colors.HexColor("#334155")
        )

        muted_style = ParagraphStyle(
            "MutedStyle",
            parent=styles["Normal"],
            fontSize=8.5,
            leading=13,
            textColor=colors.HexColor("#64748B")
        )

        metric_label_style = ParagraphStyle(
            "MetricLabelStyle",
            parent=styles["Normal"],
            fontSize=8,
            leading=10,
            textColor=colors.HexColor("#64748B")
        )

        metric_value_style = ParagraphStyle(
            "MetricValueStyle",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=18,
            leading=22,
            textColor=colors.HexColor("#0F172A")
        )

        content = [
            Paragraph("ReviewGrow Reputation Report", title_style),
            Paragraph(
                "AI-generated customer review analytics for reputation monitoring and operational decision-making.",
                muted_style
            ),
            Spacer(1, 12)
        ]

        location = ", ".join(
            [
                str(value)
                for value in [
                    report.get("city"),
                    report.get("state"),
                    report.get("country")
                ]
                if value
            ]
        ) or "Not specified"

        business_data = [
            [
                Paragraph("<b>Business</b>", metric_label_style),
                Paragraph("<b>Type</b>", metric_label_style),
                Paragraph("<b>Location</b>", metric_label_style),
                Paragraph("<b>Generated</b>", metric_label_style)
            ],
            [
                Paragraph(_paragraph_text(report["business_name"]), normal_style),
                Paragraph(_paragraph_text(report["business_type"]), normal_style),
                Paragraph(_paragraph_text(location), normal_style),
                Paragraph(_paragraph_text(report["generated_at"]), normal_style)
            ]
        ]

        business_table = Table(
            business_data,
            colWidths=[1.45 * inch, 1.15 * inch, 1.65 * inch, 1.35 * inch]
        )
        business_table.setStyle(
            TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
                ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#E2E8F0")),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E2E8F0")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("RIGHTPADDING", (0, 0), (-1, -1), 9)
            ])
        )

        content.extend([
            business_table,
            Spacer(1, 14),
            Paragraph("Performance Snapshot", section_style)
        ])

        metric_data = [
            [
                Paragraph("Sentiment Score", metric_label_style),
                Paragraph("Business Health", metric_label_style),
                Paragraph("Reviews Analyzed", metric_label_style),
                Paragraph("Priority Level", metric_label_style)
            ],
            [
                Paragraph(f"{score}%", metric_value_style),
                Paragraph(business_health, metric_value_style),
                Paragraph(str(review_count), metric_value_style),
                Paragraph("High" if score < 50 else "Medium" if score < 70 else "Low", metric_value_style)
            ]
        ]

        metric_table = Table(
            metric_data,
            colWidths=[1.4 * inch, 1.45 * inch, 1.35 * inch, 1.4 * inch]
        )
        metric_table.setStyle(
            TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EFF6FF")),
                ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#BFDBFE")),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#BFDBFE")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 9),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10)
            ])
        )

        content.extend([
            metric_table,
            Spacer(1, 10),
            Paragraph(_paragraph_text(_business_health_note(score)), normal_style),
            Paragraph("Sentiment Distribution", section_style)
        ])

        distribution_data = [
            ["Sentiment", "Estimated Reviews", "Share"],
            ["Positive", str(positive_count), f"{round((positive_count / review_count) * 100, 1) if review_count else 0}%"],
            ["Neutral", str(neutral_count), f"{round((neutral_count / review_count) * 100, 1) if review_count else 0}%"],
            ["Negative", str(negative_count), f"{round((negative_count / review_count) * 100, 1) if review_count else 0}%"]
        ]

        distribution_table = Table(
            distribution_data,
            colWidths=[2.15 * inch, 1.75 * inch, 1.7 * inch]
        )
        distribution_table.setStyle(
            TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0F172A")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#ECFDF5")),
                ("BACKGROUND", (0, 2), (-1, 2), colors.HexColor("#FFFBEB")),
                ("BACKGROUND", (0, 3), (-1, 3), colors.HexColor("#FEF2F2")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CBD5E1")),
                ("ALIGN", (1, 1), (-1, -1), "CENTER"),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8)
            ])
        )

        content.extend([
            distribution_table,
            Spacer(1, 10),
            Paragraph("Executive Summary", section_style),
            Paragraph(_paragraph_text(report.get("summary")), normal_style),
            Paragraph("Customer Feedback Themes", section_style)
        ])

        theme_data = [
            [
                Paragraph("<b>Top Customer Praises</b>", normal_style),
                Paragraph("<b>Top Customer Complaints</b>", normal_style)
            ]
        ]

        max_items = max(len(_safe_list(praises, "No major praises detected.")), len(_safe_list(complaints, "No major complaints detected.")))
        safe_praises = _safe_list(praises, "No major praises detected.")
        safe_complaints = _safe_list(complaints, "No major complaints detected.")

        for index in range(max_items):
            praise = safe_praises[index] if index < len(safe_praises) else ""
            complaint = safe_complaints[index] if index < len(safe_complaints) else ""
            theme_data.append([
                Paragraph(f"{index + 1}. {_paragraph_text(praise)}", normal_style),
                Paragraph(f"{index + 1}. {_paragraph_text(complaint)}", normal_style)
            ])

        theme_table = Table(theme_data, colWidths=[2.75 * inch, 2.85 * inch])
        theme_table.setStyle(
            TableStyle([
                ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#ECFDF5")),
                ("BACKGROUND", (1, 0), (1, 0), colors.HexColor("#FEF2F2")),
                ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#E2E8F0")),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E2E8F0")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("RIGHTPADDING", (0, 0), (-1, -1), 9)
            ])
        )

        content.extend([
            theme_table,
            Paragraph("AI Business Recommendations", section_style)
        ])

        recommendation_data = [["#", "Recommended Action"]]

        for index, item in enumerate(_safe_list(recommendations, "No recommendations available."), start=1):
            recommendation_data.append([
                str(index),
                Paragraph(_paragraph_text(item), normal_style)
            ])

        recommendation_table = Table(
            recommendation_data,
            colWidths=[0.45 * inch, 5.15 * inch]
        )
        recommendation_table.setStyle(
            TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2563EB")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("ALIGN", (0, 1), (0, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CBD5E1")),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8)
            ])
        )

        content.extend([
            recommendation_table,
            Paragraph("How to Read This Report", section_style),
            Paragraph(
                "The sentiment score is an AI-generated reputation indicator from uploaded customer reviews. "
                "Positive, neutral, and negative review counts are estimated from the score and review volume for quick executive interpretation.",
                muted_style
            ),
            Spacer(1, 12),
            Paragraph("Generated by ReviewGrow AI Reputation Manager · (c) 2026 ReviewGrow", muted_style)
        ])

        doc.build(content)
        buffer.seek(0)

        return send_file(
            buffer,
            as_attachment=True,
            download_name="reputation_report.pdf",
            mimetype="application/pdf"
        )

    except Exception as e:
        return {"message": str(e)}, 500


@dashboard_bp.route("/dashboard/<int:business_id>")
@subscription_required
def dashboard(business_id):
    if "user_id" not in session:
        return redirect("/login-page")

    if not _can_access_business(business_id):
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
            """,
            (report_id, business_id)
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
            ORDER BY r.generated_at DESC
            LIMIT 1
            """,
            (business_id,)
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

    cursor.execute(
        """
        SELECT
            id,
            status,
            total_reviews,
            processed_reviews,
            failed_reviews,
            error_message,
            latest_report_id,
            created_at,
            completed_at
        FROM analysis_jobs
        WHERE business_id=%s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (business_id,)
    )
    latest_analysis_job = cursor.fetchone()

    business = None
    if not report:
        cursor.execute(
            """
            SELECT
                id,
                business_name,
                business_type,
                city,
                state,
                country
            FROM businesses
            WHERE id=%s
            """,
            (business_id,)
        )
        business = cursor.fetchone()

    cursor.close()
    conn.close()

    if not report:
        return render_template(
            "dashboard.html",
            report=None,
            business=business,
            report_history=report_history,
            report_history_count=len(report_history),
            business_id=business_id,
            latest_analysis_job=latest_analysis_job
        )

    report["top_complaints"] = _json_list(report.get("top_complaints"))
    report["top_praises"] = _json_list(report.get("top_praises"))
    report["recommendations"] = _json_list(report.get("recommendations")) or [
        "No recommendations available"
    ]
    report["sentiment_score"] = report.get("sentiment_score") or 0
    report["review_count"] = report.get("review_count") or 0

    for item in report_history:
        if item["generated_at"]:
            item["generated_at"] = item["generated_at"].strftime("%d %b %Y %I:%M %p")

    return render_template(
        "dashboard.html",
        report=report,
        report_history=report_history,
        report_history_count=len(report_history),
        business_id=business_id,
        latest_analysis_job=latest_analysis_job
    )
