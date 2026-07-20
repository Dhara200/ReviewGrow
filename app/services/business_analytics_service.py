import logging

from app.services.business_metrics_service import calculate_business_metrics
from app.services.database_service import get_connection
from app.services.review_topic_service import extract_topics_for_business


logger = logging.getLogger(__name__)


def refresh_business_review_analytics(
    business_id,
    mark_consultant_outdated=False,
    max_gemini_topic_reviews=25,
    source=None,
    google_location_id=None,
    require_google_review_id=False,
):
    """
    Rebuilds review-derived analytics from the stored reviews table.

    This is intentionally Google-agnostic: Google sync, manual upload,
    dashboards, sentiment analytics, and AI Consultant should all call/read this
    layer instead of duplicating review calculations or fetching source data.
    """
    topic_result = {"processed_reviews": 0, "inserted_topics": 0}

    try:
        topic_result = extract_topics_for_business(
            business_id,
            max_gemini_reviews=max_gemini_topic_reviews,
            source=source,
            google_location_id=google_location_id,
            require_google_review_id=require_google_review_id,
        )
    except Exception:
        logger.exception(
            "Business topic analytics refresh failed for business_id=%s",
            business_id,
        )

    metrics = calculate_business_metrics(
        business_id,
        source=source,
        google_location_id=google_location_id,
        require_google_review_id=require_google_review_id,
    )

    if mark_consultant_outdated:
        mark_ai_consultant_report_outdated(business_id)

    return {
        "metrics": metrics,
        "topic_result": topic_result,
    }


def mark_ai_consultant_report_outdated(business_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            UPDATE ai_consultant_reports
            SET report_status='outdated',
                outdated_at=NOW()
            WHERE business_id=%s
            AND report_status <> 'outdated'
            """,
            (business_id,)
        )
        conn.commit()
        return cursor.rowcount
    finally:
        cursor.close()
        conn.close()


def get_business_review_metrics(
    business_id,
    source=None,
    google_location_id=None,
    require_google_review_id=False,
):
    return calculate_business_metrics(
        business_id,
        source=source,
        google_location_id=google_location_id,
        require_google_review_id=require_google_review_id,
    )


def get_google_review_snapshot(
    business_id,
    google_location_id=None,
    limit=25,
    offset=0,
    page=1,
    filters=None,
):
    if not google_location_id:
        return _empty_google_review_stats(), [], _empty_review_filter_summary(), []

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    filters = filters or {}
    scope_sql, scope_params = _google_review_scope_sql(business_id, google_location_id, filters)
    filter_sql, filter_params = _google_review_filter_sql(filters)

    try:
        cursor.execute(
            f"""
            SELECT
                r.id,
                r.reviewer_name,
                r.review_text,
                COALESCE(r.review_rating, r.rating) AS rating,
                r.review_created_at,
                r.review_updated_at,
                r.sentiment,
                r.summary,
                r.suggested_reply,
                r.reply_status,
                r.reply_generated_at,
                r.reply_posted_at,
                r.reply_error_message,
                r.google_review_id,
                r.external_review_id,
                (
                    SELECT rt.topic
                    FROM review_topics rt
                    WHERE rt.review_id = r.id
                    ORDER BY rt.confidence DESC
                    LIMIT 1
                ) AS topic
            FROM reviews r
            {scope_sql}
            {filter_sql}
            ORDER BY COALESCE(review_updated_at, review_created_at, review_date, created_at) DESC,
                     r.id DESC
            LIMIT %s OFFSET %s
            """,
            tuple([*scope_params, *filter_params, limit, offset])
        )
        reviews = cursor.fetchall()
        cursor.execute(
            f"""
            SELECT COUNT(*) AS filtered_count
            FROM reviews r
            {scope_sql}
            {filter_sql}
            """,
            tuple([*scope_params, *filter_params])
        )
        filtered_count = int((cursor.fetchone() or {}).get("filtered_count") or 0)
        summary = _google_review_filter_summary(cursor, business_id, google_location_id)
        summary["filtered_reviews"] = filtered_count
        summary["page"] = page
        summary["per_page"] = limit
        summary["total_pages"] = (
            (filtered_count + limit - 1) // limit if filtered_count else 0
        )
        urgent_reviews = _urgent_google_reviews(cursor, business_id, google_location_id)
    finally:
        cursor.close()
        conn.close()

    metrics = calculate_business_metrics(
        business_id,
        source="google",
        google_location_id=google_location_id,
    )
    stats = {
        "total_reviews": metrics["total_reviews"],
        "average_rating": metrics["average_rating"],
        "positive_reviews": metrics["positive_review_count"],
        "neutral_reviews": metrics["neutral_review_count"],
        "negative_reviews": metrics["negative_review_count"],
    }
    return stats, reviews, summary, urgent_reviews


def _google_review_filter_sql(filters):
    clauses = []
    params = []
    rating = filters.get("rating")
    if rating in {"1", "2", "3", "4", "5"}:
        clauses.append("AND ROUND(COALESCE(r.review_rating, r.rating))=%s")
        params.append(int(rating))

    if filters.get("sentiment") == "negative":
        clauses.append(
            """
            AND (
                COALESCE(r.review_rating, r.rating) <= 2
                OR LOWER(COALESCE(r.sentiment, '')) = 'negative'
            )
            """
        )
    elif filters.get("sentiment") in {"positive", "neutral"}:
        clauses.append("AND LOWER(COALESCE(r.sentiment, ''))=%s")
        params.append(filters["sentiment"])

    if filters.get("reply_status") == "unanswered":
        clauses.append(
            """
            AND (
                r.reply_status IS NULL
                OR r.reply_status IN ('pending','failed')
            )
            """
        )
    elif filters.get("reply_status") == "replied":
        clauses.append("AND r.reply_status IN ('approved','posted')")

    search = (filters.get("search") or "").strip()
    if search:
        clauses.append(
            """
            AND (
                r.review_text LIKE %s
                OR r.reviewer_name LIKE %s
            )
            """
        )
        like = f"%{search}%"
        params.extend([like, like])

    period = filters.get("period")
    if period == "today":
        clauses.append("AND DATE(COALESCE(r.review_created_at, r.review_date, r.created_at)) = CURDATE()")
    elif period == "week":
        clauses.append("AND COALESCE(r.review_created_at, r.review_date, r.created_at) >= DATE_SUB(NOW(), INTERVAL 7 DAY)")
    elif period == "month":
        clauses.append("AND COALESCE(r.review_created_at, r.review_date, r.created_at) >= DATE_SUB(NOW(), INTERVAL 30 DAY)")
    elif period == "custom":
        date_from = filters.get("date_from")
        date_to = filters.get("date_to")
        if date_from:
            clauses.append("AND DATE(COALESCE(r.review_created_at, r.review_date, r.created_at)) >= %s")
            params.append(date_from)
        if date_to:
            clauses.append("AND DATE(COALESCE(r.review_created_at, r.review_date, r.created_at)) <= %s")
            params.append(date_to)

    return "\n".join(clauses), params


def _google_review_scope_sql(business_id, google_location_id, filters):
    return (
        "WHERE r.business_id=%s AND r.source='google' AND r.google_location_id=%s",
        [business_id, google_location_id],
    )


def _google_review_filter_summary(cursor, business_id, google_location_id):
    cursor.execute(
        """
        SELECT
            COUNT(*) AS total_reviews,
            SUM(CASE WHEN ROUND(COALESCE(review_rating, rating))=5 THEN 1 ELSE 0 END) AS five_star_reviews,
            SUM(CASE WHEN ROUND(COALESCE(review_rating, rating))=4 THEN 1 ELSE 0 END) AS four_star_reviews,
            SUM(CASE WHEN ROUND(COALESCE(review_rating, rating))=3 THEN 1 ELSE 0 END) AS three_star_reviews,
            SUM(CASE WHEN ROUND(COALESCE(review_rating, rating))=2 THEN 1 ELSE 0 END) AS two_star_reviews,
            SUM(CASE WHEN ROUND(COALESCE(review_rating, rating))=1 THEN 1 ELSE 0 END) AS one_star_reviews,
            SUM(CASE
                WHEN COALESCE(review_rating, rating) <= 2
                OR LOWER(COALESCE(sentiment, '')) = 'negative'
                THEN 1 ELSE 0
            END) AS negative_reviews,
            SUM(CASE
                WHEN reply_status IS NULL
                OR reply_status IN ('pending','failed')
                THEN 1 ELSE 0
            END) AS unanswered_reviews
        FROM reviews
        WHERE business_id=%s
        AND source='google'
        AND google_location_id=%s
        """,
        (business_id, google_location_id)
    )
    row = cursor.fetchone() or {}
    return {
        "total_reviews": int(row.get("total_reviews") or 0),
        "five_star_reviews": int(row.get("five_star_reviews") or 0),
        "four_star_reviews": int(row.get("four_star_reviews") or 0),
        "three_star_reviews": int(row.get("three_star_reviews") or 0),
        "two_star_reviews": int(row.get("two_star_reviews") or 0),
        "one_star_reviews": int(row.get("one_star_reviews") or 0),
        "negative_reviews": int(row.get("negative_reviews") or 0),
        "unanswered_reviews": int(row.get("unanswered_reviews") or 0),
        "filtered_reviews": int(row.get("total_reviews") or 0),
    }


def _urgent_google_reviews(cursor, business_id, google_location_id):
    cursor.execute(
        """
        SELECT
            r.id,
            r.reviewer_name,
            r.review_text,
            COALESCE(r.review_rating, r.rating) AS rating,
            r.review_created_at,
            r.review_updated_at,
            r.sentiment,
            r.reply_status,
            (
                SELECT rt.topic
                FROM review_topics rt
                WHERE rt.review_id = r.id
                ORDER BY rt.confidence DESC
                LIMIT 1
            ) AS topic
        FROM reviews r
        WHERE r.business_id=%s
        AND r.source='google'
        AND r.google_location_id=%s
        AND COALESCE(r.review_rating, r.rating) <= 2
        AND (
            r.reply_status IS NULL
            OR r.reply_status IN ('pending','failed')
        )
        ORDER BY COALESCE(r.review_updated_at, r.review_created_at, r.review_date, r.created_at) DESC
        LIMIT 5
        """,
        (business_id, google_location_id)
    )
    return cursor.fetchall()


def _empty_review_filter_summary():
    return {
        "total_reviews": 0,
        "five_star_reviews": 0,
        "four_star_reviews": 0,
        "three_star_reviews": 0,
        "two_star_reviews": 0,
        "one_star_reviews": 0,
        "negative_reviews": 0,
        "unanswered_reviews": 0,
        "filtered_reviews": 0,
    }


def _empty_google_review_stats():
    return {
        "total_reviews": 0,
        "average_rating": 0,
        "positive_reviews": 0,
        "neutral_reviews": 0,
        "negative_reviews": 0,
    }
