import json

from flask import current_app

from app.services.ai_service import AIService, AIServiceError, log_ai_usage
from app.services.business_alert_service import build_business_alerts
from app.services.business_health_service import calculate_business_health
from app.services.business_metrics_service import (
    calculate_business_metrics,
    get_emotion_breakdown,
    get_latest_attention_reviews,
    get_latest_review_timestamp,
    get_topic_analytics,
    get_trend_summary,
)
from app.services.database_service import get_connection


REPORT_LIST_FIELDS = [
    "strengths",
    "weaknesses",
    "positive_topics",
    "negative_topics",
    "priority_actions",
    "risks",
    "opportunities",
    "next_steps",
    "ai_alerts",
    "action_plan",
    "emotion_breakdown",
    "latest_attention_reviews",
]

REPORT_JSON_FIELDS = REPORT_LIST_FIELDS + ["trend_summary", "raw_ai_response"]
CONSULTANT_REVIEW_SOURCE = "google"
CONSULTANT_REPORT_SOURCE = "google_live"


def get_latest_consultant_report(business_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    usage_result = None
    try:
        cursor.execute(
            """
            SELECT *
            FROM ai_consultant_reports
            WHERE business_id=%s
            AND review_source=%s
            ORDER BY generated_at DESC
            LIMIT 1
            """,
            (business_id, CONSULTANT_REPORT_SOURCE)
        )
        report = cursor.fetchone()
        return _decode_report(report)
    finally:
        cursor.close()
        conn.close()


def get_command_center_snapshot(business_id, report=None, google_location_id=None):
    metrics = calculate_business_metrics(
        business_id,
        source=CONSULTANT_REVIEW_SOURCE,
        google_location_id=google_location_id,
        require_google_review_id=True,
    )
    topic_analytics = get_topic_analytics(
        business_id,
        source=CONSULTANT_REVIEW_SOURCE,
        google_location_id=google_location_id,
        recent_days=30,
        require_google_review_id=True,
    )
    emotion_breakdown = get_emotion_breakdown(
        business_id,
        source=CONSULTANT_REVIEW_SOURCE,
        google_location_id=google_location_id,
        require_google_review_id=True,
    )
    trend_summary = get_trend_summary(
        business_id,
        source=CONSULTANT_REVIEW_SOURCE,
        google_location_id=google_location_id,
        require_google_review_id=True,
    )
    latest_attention_reviews = get_latest_attention_reviews(
        business_id,
        source=CONSULTANT_REVIEW_SOURCE,
        google_location_id=google_location_id,
        recent_days=30,
        require_google_review_id=True,
    )
    latest_review_at = get_latest_review_timestamp(
        business_id,
        source=CONSULTANT_REVIEW_SOURCE,
        google_location_id=google_location_id,
        require_google_review_id=True,
    )
    health = calculate_business_health(metrics)
    alerts = build_business_alerts(metrics, trend_summary, topic_analytics)
    report_status = _report_status(report, latest_review_at)

    strengths = _simple_topic_names(metrics.get("top_positive_topics"))
    weaknesses = _simple_topic_names(metrics.get("top_negative_topics"))
    opportunities = _opportunities_from_topics(strengths)
    risks = _risks_from_topics(weaknesses, trend_summary)
    action_plan = _action_plan(metrics, weaknesses, latest_attention_reviews, trend_summary)
    daily_briefing = _daily_briefing(metrics, strengths, weaknesses, trend_summary)
    executive_summary = _executive_summary(metrics, strengths, weaknesses, trend_summary)

    return {
        "metrics": metrics,
        "health": health,
        "daily_briefing": daily_briefing,
        "executive_summary": executive_summary,
        "alerts": alerts,
        "action_plan": action_plan,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "topic_analytics": topic_analytics,
        "emotion_breakdown": emotion_breakdown,
        "trend_summary": trend_summary,
        "latest_attention_reviews": latest_attention_reviews,
        "opportunities": opportunities,
        "risks": risks,
        "latest_review_at": latest_review_at,
        "report_status": report_status,
        "data_source": "Live Google Reviews Only",
    }


def generate_consultant_report(
    business_id,
    user_id,
    google_location_id=None,
    ownership_check=None,
    job_context=None,
    fallback_on_provider_error=True,
):
    snapshot = get_command_center_snapshot(business_id, google_location_id=google_location_id)
    metrics = snapshot["metrics"]
    samples = _sample_reviews(business_id, google_location_id=google_location_id)
    business = _business_context(business_id)
    source_counts = _source_exclusion_counts(
        business_id,
        google_location_id=google_location_id,
    )
    current_app.logger.info(
        "AI Consultant source scope: business_id=%s google_review_count=%s excel_review_count_excluded=%s manual_review_count_excluded=%s null_source_review_count_excluded=%s",
        business_id,
        source_counts["google_review_count"],
        source_counts["excel_review_count_excluded"],
        source_counts["manual_review_count_excluded"],
        source_counts["null_source_review_count_excluded"],
    )

    if metrics["total_reviews"] < 5:
        raise ValueError("Need at least 5 live Google reviews to generate reliable consultant insights.")

    try:
        ai_report, ai_result = _generate_with_gemini(business, snapshot, samples)
        report = _normalize_ai_report(ai_report, snapshot)
        report["raw_ai_response"] = ai_report
        report["ai_fallback_used"] = False
    except Exception as error:
        current_app.logger.exception(
            "AI consultant Gemini generation failed for business_id=%s",
            business_id,
        )
        if not fallback_on_provider_error:
            raise
        result = getattr(error, "result", None)
        if isinstance(error, AIServiceError) and error.result:
            result = error.result
        usage_result = result

        report = _fallback_report(business, snapshot)
        report["raw_ai_response"] = {
            "fallback": True,
            "reason": "ai_generation_failed",
        }
        report["ai_fallback_used"] = True
    else:
        usage_result = ai_result

    if ownership_check is not None and not ownership_check():
        raise RuntimeError("AI job ownership was lost before consultant persistence.")

    if job_context:
        saved_report = _save_owned_job_result(
            business_id, user_id, report, usage_result, job_context
        )
    else:
        if usage_result:
            _safe_log_ai_usage(user_id, business_id, usage_result)
        saved_report = _save_report(business_id, report)
    saved_report["ai_fallback_used"] = report.get("ai_fallback_used", False)
    return saved_report


def _generate_with_gemini(business, snapshot, samples):
    compact_payload = {
        "metrics": snapshot["metrics"],
        "health": snapshot["health"],
        "topic_analytics": snapshot["topic_analytics"],
        "sentiment_breakdown": {
            "positive": snapshot["metrics"].get("positive_review_percentage"),
            "neutral": snapshot["metrics"].get("neutral_review_percentage"),
            "negative": snapshot["metrics"].get("negative_review_percentage"),
        },
        "emotion_breakdown": snapshot["emotion_breakdown"],
        "trend_summary": snapshot["trend_summary"],
        "latest_attention_reviews": snapshot["latest_attention_reviews"],
    }
    prompt = f"""
You are an AI Business Command Center for a local business owner.
Use the provided review metrics to produce specific, practical, operational advice.
Do not sound like a generic chatbot.

Business:
{json.dumps(business, ensure_ascii=False)}

Compact analytics from live Google reviews only:
{json.dumps(compact_payload, ensure_ascii=False, default=str)}

Sample positive Google reviews, maximum 5:
{json.dumps(samples["positive"], ensure_ascii=False)}

Sample negative Google reviews, maximum 5:
{json.dumps(samples["negative"], ensure_ascii=False)}

Return ONLY valid JSON in this exact shape:
{{
  "overall_score": 8.1,
  "health_status": "Excellent | Good | Needs Attention | Critical",
  "executive_summary": "2-4 specific sentences based on the data",
  "daily_briefing": "short daily briefing",
  "ai_alerts": [
    {{"title": "Reply to unanswered 1-star reviews", "message": "why this matters", "priority": "High", "type": "danger"}}
  ],
  "action_plan": [
    {{"title": "Reply to 6 negative reviews", "reason": "why", "priority": "High", "impact": "High", "owner_action": "what to do today"}}
  ],
  "strengths": ["specific strength tied to ratings/topics"],
  "weaknesses": ["specific weakness tied to ratings/topics"],
  "positive_topics": ["topic: explanation"],
  "negative_topics": ["topic: explanation"],
  "emotion_breakdown": [
    {{"label": "Happy", "count": 5, "percentage": 20}}
  ],
  "trend_summary": {{}},
  "latest_attention_reviews": [],
  "priority_actions": ["specific action with owner-ready next step"],
  "risks": ["specific risk if ignored"],
  "opportunities": ["specific growth opportunity"],
  "next_steps": ["specific next step for the next 7-30 days"]
}}

Rules:
- overall_score must be 0 to 10.
- health_status must be one of Excellent, Good, Needs Attention, Critical.
- priority_actions must contain exactly 5 items.
- action_plan must contain exactly 5 owner-ready actions.
- Recommendations must be concrete and operational.
- Mention review/rating trends where useful.
- Do not invent facts not supported by the metrics or samples.
- Avoid vague advice like "Improve service"; explain what to do, when, and why.
"""
    result = AIService().generate_json(prompt, "ai_business_consultant", max_retries=1)
    return result.data, result


def _fallback_report(business, snapshot):
    metrics = snapshot["metrics"]
    health = snapshot["health"]
    score = health["score"] if health["score"] is not None else 0
    health_status = health["status"] if health["status"] != "Pending" else "Needs Attention"
    positive_topics = metrics.get("top_positive_topics") or []
    negative_topics = metrics.get("top_negative_topics") or []
    main_positive = positive_topics[0]["topic"] if positive_topics else "customer experience"
    main_negative = negative_topics[0]["topic"] if negative_topics else "repeat complaints"
    rating_change = metrics.get("rating_change") or 0

    trend_text = (
        f"Average rating moved by {rating_change:+.2f} points this month."
        if rating_change
        else "Monthly rating movement is currently flat or does not have enough dated reviews."
    )

    return {
        "overall_score": score,
        "health_status": health_status,
        "executive_summary": snapshot["executive_summary"],
        "daily_briefing": snapshot["daily_briefing"],
        "ai_alerts": snapshot["alerts"],
        "action_plan": snapshot["action_plan"],
        "strengths": _topic_sentences(
            positive_topics,
            "Customers repeatedly mention {topic} positively, so keep this standard visible in staff training and marketing."
        ) or ["Customers are leaving enough feedback to identify repeat strengths and build a clear reputation plan."],
        "weaknesses": _topic_sentences(
            negative_topics,
            "{topic} appears in negative reviews; assign an owner to review recent complaints and fix the repeat cause."
        ) or ["No dominant negative topic is visible yet; continue monitoring low-rating reviews for repeat patterns."],
        "positive_topics": _topic_labels(positive_topics),
        "negative_topics": _topic_labels(negative_topics),
        "priority_actions": _priority_actions(metrics, main_negative),
        "risks": [
            f"If {main_negative} complaints continue, future customers may see the issue as a pattern rather than an exception.",
            "Ignoring recent negative reviews can reduce trust because unresolved complaints remain public.",
            "A weak response rate can make the business appear less attentive even when operations are improving.",
        ],
        "opportunities": [
            f"Use positive {main_positive} feedback in website, Google posts, and staff recognition.",
            "Ask satisfied customers for reviews after successful service moments to increase recent positive volume.",
            "Turn common complaints into visible process improvements and mention fixes in review replies.",
        ],
        "next_steps": [
            "Review the last 10 low-rating reviews and tag each one with an owner and fix date.",
            "Reply to pending negative reviews within 24 hours using a calm, specific service-recovery response.",
            "Share the top positive theme with the team and define the behavior that should be repeated.",
            "Track this report again after 30 days to confirm rating and topic movement.",
        ],
        "emotion_breakdown": snapshot["emotion_breakdown"],
        "trend_summary": snapshot["trend_summary"],
        "latest_attention_reviews": snapshot["latest_attention_reviews"],
        "last_review_synced_at": snapshot["latest_review_at"],
    }


def _priority_actions(metrics, main_negative):
    actions = [
        f"Create a 7-day fix plan for {main_negative}: list the top complaint examples, assign one owner, and review progress weekly.",
        "Respond to every 1-3 star review within 24 hours with an apology, one specific acknowledgement, and an offline contact path.",
        "Ask staff to record the reason behind each new complaint for two weeks so the business can separate one-off issues from repeat process failures.",
        "Request reviews from happy customers immediately after successful interactions to increase recent positive review volume.",
        "Compare this month's rating with the previous month every week and investigate any drop of 0.2 stars or more.",
    ]

    if metrics.get("unanswered_review_count", 0) > 0:
        actions[1] = (
            f"Clear {metrics['unanswered_review_count']} pending review replies, starting with negative reviews, "
            "then maintain a 24-hour response habit."
        )

    return actions


def _sample_reviews(business_id, google_location_id=None):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    filters = [
        "business_id=%s",
        "source=%s",
        "review_text IS NOT NULL",
        "TRIM(review_text) <> ''",
    ]
    params = [business_id, CONSULTANT_REVIEW_SOURCE]

    if google_location_id:
        filters.append("google_location_id=%s")
        params.append(google_location_id)
    filters.append("google_review_id IS NOT NULL")

    try:
        base_select = f"""
            SELECT
                COALESCE(review_rating, rating) AS rating,
                review_text,
                COALESCE(review_created_at, review_date, created_at) AS review_date
            FROM reviews
            WHERE {" AND ".join(filters)}
        """
        cursor.execute(
            base_select + """
            AND COALESCE(review_rating, rating) >= 4
            ORDER BY COALESCE(review_created_at, review_date, created_at) DESC
            LIMIT 5
            """,
            tuple(params)
        )
        positive = cursor.fetchall()

        cursor.execute(
            base_select + """
            AND COALESCE(review_rating, rating) <= 2
            ORDER BY COALESCE(review_created_at, review_date, created_at) DESC
            LIMIT 5
            """,
            tuple(params)
        )
        negative = cursor.fetchall()

        return {
            "positive": _serialize_samples(positive),
            "negative": _serialize_samples(negative),
        }
    finally:
        cursor.close()
        conn.close()


def _source_exclusion_counts(business_id, google_location_id=None):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    location_filter = ""
    params = [business_id]

    if google_location_id:
        location_filter = "AND (google_location_id=%s OR google_location_id IS NULL)"
        params.append(google_location_id)

    try:
        cursor.execute(
            f"""
            SELECT
                SUM(CASE WHEN source='google' AND google_review_id IS NOT NULL THEN 1 ELSE 0 END) AS google_review_count,
                SUM(CASE WHEN source='excel' THEN 1 ELSE 0 END) AS excel_review_count_excluded,
                SUM(CASE WHEN source='manual' THEN 1 ELSE 0 END) AS manual_review_count_excluded,
                SUM(CASE WHEN source IS NULL OR source='' THEN 1 ELSE 0 END) AS null_source_review_count_excluded
            FROM reviews
            WHERE business_id=%s
            {location_filter}
            """,
            tuple(params)
        )
        row = cursor.fetchone() or {}
        return {
            "google_review_count": int(row.get("google_review_count") or 0),
            "excel_review_count_excluded": int(row.get("excel_review_count_excluded") or 0),
            "manual_review_count_excluded": int(row.get("manual_review_count_excluded") or 0),
            "null_source_review_count_excluded": int(row.get("null_source_review_count_excluded") or 0),
        }
    finally:
        cursor.close()
        conn.close()


def _business_context(business_id):
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
        return cursor.fetchone() or {}
    finally:
        cursor.close()
        conn.close()


def _save_report(business_id, report):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        report_id = _insert_report(cursor, business_id, report)
        conn.commit()

        cursor.execute(
            "SELECT * FROM ai_consultant_reports WHERE id=%s",
            (report_id,)
        )
        return _decode_report(cursor.fetchone())
    finally:
        cursor.close()
        conn.close()


def _insert_report(cursor, business_id, report):
    cursor.execute(
            """
            INSERT INTO ai_consultant_reports
            (
                business_id,
                overall_score,
                health_status,
                executive_summary,
                strengths,
                weaknesses,
                positive_topics,
                negative_topics,
                priority_actions,
                risks,
                opportunities,
                next_steps,
                raw_ai_response,
                daily_briefing,
                ai_alerts,
                action_plan,
                emotion_breakdown,
                trend_summary,
                latest_attention_reviews,
                last_review_synced_at,
                review_source,
                report_status,
                outdated_at
            )
            VALUES
            (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'up_to_date',NULL)
            """,
            (
                business_id,
                report["overall_score"],
                report["health_status"],
                report["executive_summary"],
                json.dumps(report["strengths"]),
                json.dumps(report["weaknesses"]),
                json.dumps(report["positive_topics"]),
                json.dumps(report["negative_topics"]),
                json.dumps(report["priority_actions"]),
                json.dumps(report["risks"]),
                json.dumps(report["opportunities"]),
                json.dumps(report["next_steps"]),
                json.dumps(report["raw_ai_response"]),
                report.get("daily_briefing"),
                json.dumps(report.get("ai_alerts", []), default=str),
                json.dumps(report.get("action_plan", []), default=str),
                json.dumps(report.get("emotion_breakdown", []), default=str),
                json.dumps(report.get("trend_summary", {}), default=str),
                json.dumps(report.get("latest_attention_reviews", []), default=str),
                report.get("last_review_synced_at"),
                CONSULTANT_REPORT_SOURCE,
            )
        )
    return cursor.lastrowid


def _save_owned_job_result(business_id, user_id, report, usage_result, job_context):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        conn.start_transaction()
        cursor.execute(
            """
            SELECT id FROM analysis_jobs
            WHERE id=%s AND business_id=%s AND user_id=%s
              AND status='processing' AND worker_id=%s
              AND lease_expires_at > UTC_TIMESTAMP(6)
            FOR UPDATE
            """,
            (
                job_context["id"], business_id, user_id,
                job_context["worker_id"],
            ),
        )
        if not cursor.fetchone():
            conn.rollback()
            raise RuntimeError("AI job ownership was lost before consultant persistence.")

        if usage_result:
            log_ai_usage(cursor, user_id, business_id, usage_result)
        report_id = _insert_report(cursor, business_id, report)
        cursor.execute(
            """
            UPDATE analysis_jobs
            SET status='completed',completed_at=UTC_TIMESTAMP(6),
                result_consultant_report_id=%s,active_operation_key=NULL,
                worker_id=NULL,lease_expires_at=NULL,heartbeat_at=NULL,
                error_message=NULL
            WHERE id=%s AND status='processing' AND worker_id=%s
            """,
            (report_id, job_context["id"], job_context["worker_id"]),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            raise RuntimeError("AI job ownership was lost before consultant completion.")
        cursor.execute("SELECT * FROM ai_consultant_reports WHERE id=%s", (report_id,))
        saved_report = _decode_report(cursor.fetchone())
        conn.commit()
        return saved_report
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def _normalize_ai_report(data, snapshot):
    metrics = snapshot["metrics"]
    health = snapshot["health"]
    fallback_score = health["score"] if health["score"] is not None else _score_from_metrics(metrics)
    score = max(0, min(float(data.get("overall_score", fallback_score)), 10))
    main_negative = (
        metrics.get("top_negative_topics", [{}])[0].get("topic")
        if metrics.get("top_negative_topics")
        else "repeat complaints"
    )
    priority_actions = _list_field(data.get("priority_actions"), 5)
    for action in _priority_actions(metrics, main_negative):
        if len(priority_actions) >= 5:
            break
        if action not in priority_actions:
            priority_actions.append(action)

    executive_summary = str(data.get("executive_summary") or "").strip()
    if not executive_summary:
        executive_summary = (
            f"The business has {metrics.get('total_reviews', 0)} reviews with an average rating of "
            f"{metrics.get('average_rating', 0):.2f}. Use the priority actions below to protect strengths, "
            "fix repeated complaints, and improve recent rating momentum."
        )

    return {
        "overall_score": round(score, 2),
        "health_status": _clean_health_status(data.get("health_status"), score),
        "executive_summary": executive_summary,
        "daily_briefing": str(data.get("daily_briefing") or snapshot["daily_briefing"]).strip(),
        "ai_alerts": _object_list_field(data.get("ai_alerts"), snapshot["alerts"], 6),
        "action_plan": _object_list_field(data.get("action_plan"), snapshot["action_plan"], 5),
        "strengths": _list_field(data.get("strengths"), 5),
        "weaknesses": _list_field(data.get("weaknesses"), 5),
        "positive_topics": _list_field(data.get("positive_topics"), 8),
        "negative_topics": _list_field(data.get("negative_topics"), 8),
        "priority_actions": priority_actions[:5],
        "risks": _list_field(data.get("risks"), 5),
        "opportunities": _list_field(data.get("opportunities"), 5),
        "next_steps": _list_field(data.get("next_steps"), 6),
        "emotion_breakdown": _object_list_field(
            data.get("emotion_breakdown"),
            snapshot["emotion_breakdown"],
            6,
        ),
        "trend_summary": (
            _json_value(data.get("trend_summary"))
            if isinstance(_json_value(data.get("trend_summary")), dict)
            and _json_value(data.get("trend_summary"))
            else snapshot["trend_summary"]
        ),
        "latest_attention_reviews": _object_list_field(
            data.get("latest_attention_reviews"),
            snapshot["latest_attention_reviews"],
            5,
        ),
        "last_review_synced_at": snapshot["latest_review_at"],
    }


def _decode_report(report):
    if not report:
        return None

    for field in REPORT_LIST_FIELDS:
        report[field] = _json_list(report.get(field))
    report["trend_summary"] = _json_value(report.get("trend_summary")) or {}
    report["raw_ai_response"] = _json_value(report.get("raw_ai_response"))
    report["overall_score"] = float(report.get("overall_score") or 0)
    return report


def _serialize_samples(rows):
    return [
        {
            "rating": float(row["rating"] or 0),
            "review_text": row["review_text"][:700],
            "review_date": str(row["review_date"] or ""),
        }
        for row in rows
    ]


def _score_from_metrics(metrics):
    average_rating = float(metrics.get("average_rating") or 0)
    score = average_rating * 2
    total = metrics.get("total_reviews") or 0
    negative_share = (metrics.get("negative_review_count") or 0) / total if total else 0
    score -= negative_share * 1.5
    if metrics.get("rating_change", 0) < 0:
        score -= min(abs(metrics["rating_change"]), 1)
    return round(max(0, min(score, 10)), 2)


def _health_status(score):
    if score >= 8:
        return "Excellent"
    if score >= 6.5:
        return "Good"
    if score >= 4:
        return "Needs Attention"
    return "Critical"


def _clean_health_status(value, score):
    value = str(value or "").strip()
    allowed = {"Excellent", "Good", "Needs Attention", "Critical"}
    return value if value in allowed else _health_status(score)


def _topic_sentences(topics, template):
    return [
        template.format(topic=item["topic"]).replace("  ", " ")
        for item in topics[:5]
    ]


def _topic_labels(topics):
    return [
        f"{item['topic']}: mentioned in {item['count']} review{'s' if item['count'] != 1 else ''}"
        for item in topics[:8]
    ]


def _simple_topic_names(topics):
    return [item["topic"] for item in (topics or [])[:5]]


def _executive_summary(metrics, strengths, weaknesses, trend_summary):
    rating_change = trend_summary.get("last_30_days", {}).get("rating_change") or 0
    trend_text = "improving" if rating_change > 0 else "declining" if rating_change < 0 else "stable"
    strength_text = ", ".join(strengths[:2]) if strengths else "customer experience"
    weakness_text = ", ".join(weaknesses[:2]) if weaknesses else "no dominant weakness yet"
    unanswered = int(metrics.get("unanswered_negative_review_count") or 0)

    action_text = (
        f"Reply to {unanswered} unanswered negative review{'s' if unanswered != 1 else ''} today"
        if unanswered else
        "Keep monitoring new low-rating reviews and ask satisfied customers for fresh reviews"
    )
    return (
        f"Your reputation is {trend_text} with an average rating of {metrics.get('average_rating', 0):.1f}. "
        f"Customers most often praise {strength_text}, while {weakness_text} needs attention. "
        f"{action_text}."
    )


def _daily_briefing(metrics, strengths, weaknesses, trend_summary):
    recent_count = int(metrics.get("recent_review_count") or 0)
    if recent_count == 0:
        return "No new review activity detected recently. Keep Google sync active and continue asking satisfied customers for reviews."

    rating_change = trend_summary.get("last_30_days", {}).get("rating_change") or 0
    loved = strengths[0] if strengths else "the experience"
    concern = weaknesses[0] if weaknesses else "no repeated complaint"
    recommended = (
        f"reply to {metrics.get('unanswered_negative_review_count')} negative review(s) today"
        if metrics.get("unanswered_negative_review_count") else
        "reinforce the top positive topic in replies and marketing"
    )
    return (
        f"Good morning. You received {recent_count} recent review{'s' if recent_count != 1 else ''}. "
        f"Rating moved {rating_change:+.1f} versus the previous 30 days. "
        f"Customers loved {loved}; repeated concern: {concern}. Recommended action: {recommended}."
    )


def _opportunities_from_topics(strengths):
    opportunities = []
    for topic in strengths[:5]:
        if topic in {"staff", "service", "customer support"}:
            opportunities.append("Highlight staff friendliness in review replies, Google posts, and your business description.")
        elif topic in {"food", "ambience"}:
            opportunities.append(f"Use high-quality {topic} photos in Google Posts and social marketing.")
        elif topic in {"room", "cleanliness"}:
            opportunities.append(f"Use {topic} as a selling point in listings, captions, and reply templates.")
        elif topic == "location":
            opportunities.append("Promote location convenience in your Google Business Profile description.")
        else:
            opportunities.append(f"Turn positive {topic} mentions into marketing proof in posts and replies.")
    return opportunities or ["Collect more reviews so the system can identify specific growth opportunities."]


def _risks_from_topics(weaknesses, trend_summary):
    increased = trend_summary.get("last_30_days", {}).get("most_increased_negative_topic")
    risks = []
    if increased:
        topic = increased.get("topic")
        risks.append(
            f"{topic.title()} complaints are increasing. If ignored, future customers may choose competitors that appear easier or safer."
        )
    for topic in weaknesses[:4]:
        risks.append(
            f"Repeated {topic} complaints can make the issue look systemic, even if it only affects a few customers."
        )
    return risks or ["No major risk pattern is clear yet. Continue syncing reviews so weak signals are detected early."]


def _action_plan(metrics, weaknesses, attention_reviews, trend_summary):
    main_weakness = weaknesses[0] if weaknesses else "customer experience"
    unanswered_negative = int(metrics.get("unanswered_negative_review_count") or 0)
    rating_change = trend_summary.get("last_30_days", {}).get("rating_change") or 0
    actions = []

    if unanswered_negative:
        actions.append({
            "title": f"Reply to {unanswered_negative} negative review{'s' if unanswered_negative != 1 else ''}",
            "reason": "Negative reviews are unanswered and may reduce customer trust.",
            "priority": "High",
            "impact": "High",
            "owner_action": "Use the AI reply generator and send responses today.",
        })

    if attention_reviews:
        actions.append({
            "title": f"Review {main_weakness} complaints",
            "reason": f"{main_weakness.title()} appears in reviews needing attention.",
            "priority": "High",
            "impact": "High",
            "owner_action": "Read the latest low-rating reviews, assign an owner, and fix the repeated cause this week.",
        })

    if rating_change < -0.2:
        actions.append({
            "title": "Investigate recent rating drop",
            "reason": f"Rating fell {abs(rating_change):.1f} points versus the previous 30 days.",
            "priority": "High",
            "impact": "High",
            "owner_action": "Compare recent negative reviews with staff schedules, wait times, and operational changes.",
        })

    actions.extend([
        {
            "title": "Ask satisfied customers for fresh reviews",
            "reason": "Recent positive reviews improve trust and offset older negative feedback.",
            "priority": "Medium",
            "impact": "Medium",
            "owner_action": "Ask happy customers immediately after a successful interaction.",
        },
        {
            "title": f"Track {main_weakness} for 30 days",
            "reason": "A repeated topic needs measurement before and after the fix.",
            "priority": "Medium",
            "impact": "Medium",
            "owner_action": "Tag new complaints by topic and review progress every week.",
        },
        {
            "title": "Promote the top customer praise",
            "reason": "Positive topics can become conversion-focused marketing proof.",
            "priority": "Low",
            "impact": "Medium",
            "owner_action": "Use the top praise in Google posts, replies, captions, and business descriptions.",
        },
    ])
    return actions[:5]


def _report_status(report, latest_review_at):
    if not report:
        return {"label": "No report yet", "state": "none"}
    if report.get("report_status") == "outdated":
        return {"label": "Update available", "state": "outdated"}
    generated_at = report.get("generated_at")
    if latest_review_at and generated_at and latest_review_at > generated_at:
        return {"label": "Update available", "state": "outdated"}
    return {"label": "Up to date", "state": "up_to_date"}


def _list_field(value, limit):
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        items = [value]
    else:
        items = []

    cleaned = [str(item).strip().replace("**", "") for item in items if str(item).strip()]
    return cleaned[:limit]


def _object_list_field(value, fallback, limit):
    if isinstance(value, list):
        rows = [item for item in value if isinstance(item, dict)]
    else:
        rows = []
    return (rows or fallback or [])[:limit]


def _json_list(value):
    parsed = _json_value(value)
    return parsed if isinstance(parsed, list) else []


def _json_value(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def _safe_log_ai_usage(user_id, business_id, result):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        log_ai_usage(cursor, user_id, business_id, result)
        conn.commit()
    except Exception:
        current_app.logger.exception(
            "Failed to log AI consultant usage for business_id=%s",
            business_id,
        )
    finally:
        cursor.close()
        conn.close()
