from datetime import date, datetime, timedelta

from app.services.database_service import get_connection


def get_google_review_count_trend(business_id, google_location_id=None, connected_at=None):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    today = date.today()
    start_date = _as_date(connected_at)

    try:
        if not start_date:
            start_date = _earliest_google_review_date(
                cursor,
                business_id,
                google_location_id=google_location_id,
            )

        if not start_date:
            return {
                "data": [],
                "granularity": "day",
                "start_date": None,
                "insight": "No live Google reviews available yet. Connect Google Business Profile and sync reviews to view trends.",
                "current_period_review_count": 0,
                "previous_period_review_count": 0,
                "percentage_change": 0,
            }

        if start_date > today:
            start_date = today

        duration_days = max((today - start_date).days, 0)
        granularity = _trend_granularity(duration_days)
        buckets = _trend_buckets(start_date, today, granularity)

        cursor.execute(
            """
            SELECT
                DATE(COALESCE(review_created_at, review_date, created_at)) AS review_day,
                COALESCE(review_rating, rating) AS rating
            FROM reviews
            WHERE business_id=%s
            AND source='google'
            AND google_review_id IS NOT NULL
            AND (%s IS NULL OR google_location_id=%s)
            AND DATE(COALESCE(review_created_at, review_date, created_at)) >= %s
            AND DATE(COALESCE(review_created_at, review_date, created_at)) <= %s
            ORDER BY review_day ASC
            """,
            (
                business_id,
                google_location_id,
                google_location_id,
                start_date,
                today,
            ),
        )

        for row in cursor.fetchall():
            review_day = _as_date(row.get("review_day"))
            bucket_start = _bucket_start_for_date(review_day, start_date, granularity)
            bucket = buckets.get(bucket_start)
            if not bucket:
                continue
            bucket["review_count"] += 1
            rating = _safe_float(row.get("rating"))
            if rating is not None:
                bucket["_rating_sum"] += rating
                bucket["_rating_count"] += 1

        cumulative = 0
        data = []
        for index, bucket in enumerate(buckets.values(), start=1):
            cumulative += bucket["review_count"]
            rating_count = bucket.pop("_rating_count")
            rating_sum = bucket.pop("_rating_sum")
            bucket["period_label"] = _period_label(bucket["period_start"], index, granularity)
            bucket["period_start"] = bucket["period_start"].isoformat()
            bucket["cumulative_review_count"] = cumulative
            bucket["average_rating_for_period"] = (
                round(rating_sum / rating_count, 2) if rating_count else None
            )
            data.append(bucket)

        insight, current_count, previous_count, percentage_change = _trend_insight(data)
        return {
            "data": data,
            "granularity": granularity,
            "start_date": start_date.isoformat(),
            "insight": insight,
            "current_period_review_count": current_count,
            "previous_period_review_count": previous_count,
            "percentage_change": percentage_change,
        }
    finally:
        cursor.close()
        conn.close()


def calculate_business_metrics(
    business_id,
    source=None,
    google_location_id=None,
    require_google_review_id=False,
):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    where_sql, params = _review_where_clause(
        business_id,
        source=source,
        google_location_id=google_location_id,
        require_google_review_id=require_google_review_id,
    )

    try:
        cursor.execute(
            f"""
            SELECT
                COUNT(*) AS total_reviews,
                AVG(COALESCE(review_rating, rating)) AS average_rating,
                SUM(CASE
                    WHEN LOWER(COALESCE(sentiment, '')) IN ('positive', 'positive | neutral | negative') THEN 1
                    WHEN COALESCE(review_rating, rating) >= 4 THEN 1
                    ELSE 0
                END) AS positive_review_count,
                SUM(CASE
                    WHEN LOWER(COALESCE(sentiment, '')) = 'negative' THEN 1
                    WHEN COALESCE(review_rating, rating) <= 2 THEN 1
                    ELSE 0
                END) AS negative_review_count,
                SUM(CASE
                    WHEN LOWER(COALESCE(sentiment, '')) = 'neutral' THEN 1
                    WHEN COALESCE(review_rating, rating) > 2 AND COALESCE(review_rating, rating) < 4 THEN 1
                    ELSE 0
                END) AS neutral_review_count
            FROM reviews
            {where_sql}
            """,
            tuple(params)
        )
        totals = cursor.fetchone() or {}

        current_start, previous_start = _month_boundaries()
        cursor.execute(
            f"""
            SELECT
                SUM(CASE
                    WHEN COALESCE(review_created_at, review_date, created_at) >= %s THEN 1
                    ELSE 0
                END) AS reviews_this_month,
                SUM(CASE
                    WHEN COALESCE(review_created_at, review_date, created_at) >= %s
                    AND COALESCE(review_created_at, review_date, created_at) < %s THEN 1
                    ELSE 0
                END) AS reviews_previous_month,
                AVG(CASE
                    WHEN COALESCE(review_created_at, review_date, created_at) >= %s
                    THEN COALESCE(review_rating, rating)
                    ELSE NULL
                END) AS rating_this_month,
                AVG(CASE
                    WHEN COALESCE(review_created_at, review_date, created_at) >= %s
                    AND COALESCE(review_created_at, review_date, created_at) < %s
                    THEN COALESCE(review_rating, rating)
                    ELSE NULL
                END) AS rating_previous_month
            FROM reviews
            {where_sql}
            """,
            (
                current_start,
                previous_start,
                current_start,
                current_start,
                previous_start,
                current_start,
                *params,
            )
        )
        trend = cursor.fetchone() or {}

        positive_topics = _topic_counts(
            cursor,
            business_id,
            "positive",
            source=source,
            google_location_id=google_location_id,
            require_google_review_id=require_google_review_id,
        )
        negative_topics = _topic_counts(
            cursor,
            business_id,
            "negative",
            source=source,
            google_location_id=google_location_id,
            require_google_review_id=require_google_review_id,
        )
        response_metrics = _response_metrics(cursor, where_sql, params)
        recent_metrics = _recent_metrics(cursor, where_sql, params)
        attention_counts = _attention_counts(cursor, where_sql, params)

        rating_this_month = _safe_float(trend.get("rating_this_month"))
        rating_previous_month = _safe_float(trend.get("rating_previous_month"))

        total_reviews = int(totals.get("total_reviews") or 0)
        positive_count = int(totals.get("positive_review_count") or 0)
        neutral_count = int(totals.get("neutral_review_count") or 0)
        negative_count = int(totals.get("negative_review_count") or 0)

        return {
            "total_reviews": total_reviews,
            "average_rating": round(_safe_float(totals.get("average_rating")) or 0, 2),
            "reviews_this_month": int(trend.get("reviews_this_month") or 0),
            "reviews_previous_month": int(trend.get("reviews_previous_month") or 0),
            "rating_this_month": round(rating_this_month or 0, 2),
            "rating_previous_month": round(rating_previous_month or 0, 2),
            "rating_change": round(
                (rating_this_month or 0) - (rating_previous_month or 0),
                2,
            ) if rating_this_month is not None and rating_previous_month is not None else 0,
            "positive_review_count": positive_count,
            "neutral_review_count": neutral_count,
            "negative_review_count": negative_count,
            "positive_review_percentage": _percentage(positive_count, total_reviews),
            "neutral_review_percentage": _percentage(neutral_count, total_reviews),
            "negative_review_percentage": _percentage(negative_count, total_reviews),
            "top_positive_topics": positive_topics,
            "top_negative_topics": negative_topics,
            **response_metrics,
            **recent_metrics,
            **attention_counts,
        }
    finally:
        cursor.close()
        conn.close()


def _topic_counts(
    cursor,
    business_id,
    sentiment,
    source=None,
    google_location_id=None,
    require_google_review_id=False,
):
    review_join = ""
    params = [business_id, sentiment]
    filters = []
    if source or google_location_id or require_google_review_id:
        review_join = "JOIN reviews r ON r.id = rt.review_id"
        if source:
            filters.append("AND r.source=%s")
            params.append(source)
        if google_location_id:
            filters.append("AND r.google_location_id=%s")
            params.append(google_location_id)
        if require_google_review_id:
            filters.append("AND r.google_review_id IS NOT NULL")

    cursor.execute(
        f"""
        SELECT rt.topic, COUNT(*) AS count, AVG(rt.confidence) AS confidence
        FROM review_topics rt
        {review_join}
        WHERE rt.business_id=%s
        AND rt.sentiment=%s
        {" ".join(filters)}
        GROUP BY rt.topic
        ORDER BY count DESC, confidence DESC
        LIMIT 10
        """,
        tuple(params)
    )
    return [
        {
            "topic": row["topic"],
            "count": int(row["count"] or 0),
            "confidence": round(float(row["confidence"] or 0), 2),
        }
        for row in cursor.fetchall()
    ]


def _response_metrics(cursor, where_sql, params):
    cursor.execute(
        f"""
        SELECT
            COUNT(*) AS review_count,
            SUM(CASE WHEN reply_status='pending' THEN 1 ELSE 0 END) AS unanswered_review_count,
            SUM(CASE WHEN reply_status IN ('approved','posted') THEN 1 ELSE 0 END) AS answered_review_count
        FROM reviews
        {where_sql}
        """,
        tuple(params)
    )
    row = cursor.fetchone() or {}
    review_count = int(row.get("review_count") or 0)
    answered = int(row.get("answered_review_count") or 0)

    return {
        "unanswered_review_count": int(row.get("unanswered_review_count") or 0),
        "response_rate": round((answered / review_count) * 100, 1) if review_count else 0,
    }


def get_topic_analytics(
    business_id,
    source=None,
    google_location_id=None,
    recent_days=None,
    require_google_review_id=False,
):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    where_sql, params = _review_where_clause(
        business_id,
        source=source,
        google_location_id=google_location_id,
        alias="r",
        require_google_review_id=require_google_review_id,
    )

    recent_sql = ""
    if recent_days:
        recent_sql = "AND COALESCE(r.review_created_at, r.review_date, r.created_at) >= DATE_SUB(NOW(), INTERVAL %s DAY)"
        params.append(recent_days)

    try:
        cursor.execute(
            f"""
            SELECT
                rt.topic,
                rt.sentiment,
                COUNT(*) AS count
            FROM review_topics rt
            JOIN reviews r
                ON r.id = rt.review_id
            {where_sql}
            {recent_sql}
            GROUP BY rt.topic, rt.sentiment
            ORDER BY count DESC, rt.topic
            LIMIT 20
            """,
            tuple(params)
        )
        rows = cursor.fetchall()

        total = sum(int(row["count"] or 0) for row in rows)
        return [
            {
                "topic": row["topic"],
                "sentiment": row["sentiment"],
                "count": int(row["count"] or 0),
                "percentage": _percentage(int(row["count"] or 0), total),
            }
            for row in rows
        ]
    finally:
        cursor.close()
        conn.close()


def get_emotion_breakdown(
    business_id,
    source=None,
    google_location_id=None,
    require_google_review_id=False,
):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    where_sql, params = _review_where_clause(
        business_id,
        source=source,
        google_location_id=google_location_id,
        require_google_review_id=require_google_review_id,
    )

    try:
        cursor.execute(
            f"""
            SELECT
                SUM(CASE WHEN COALESCE(review_rating, rating) >= 4.8 THEN 1 ELSE 0 END) AS delighted,
                SUM(CASE WHEN COALESCE(review_rating, rating) >= 4 AND COALESCE(review_rating, rating) < 4.8 THEN 1 ELSE 0 END) AS happy,
                SUM(CASE WHEN COALESCE(review_rating, rating) = 3 THEN 1 ELSE 0 END) AS neutral,
                SUM(CASE WHEN COALESCE(review_rating, rating) <= 1.5 THEN 1 ELSE 0 END) AS angry,
                SUM(CASE WHEN COALESCE(review_rating, rating) > 1.5 AND COALESCE(review_rating, rating) < 3 THEN 1 ELSE 0 END) AS disappointed,
                COUNT(*) AS total
            FROM reviews
            {where_sql}
            """,
            tuple(params)
        )
        row = cursor.fetchone() or {}
        total = int(row.get("total") or 0)
        labels = [
            ("Delighted", int(row.get("delighted") or 0)),
            ("Happy", int(row.get("happy") or 0)),
            ("Neutral", int(row.get("neutral") or 0)),
            ("Angry", int(row.get("angry") or 0)),
            ("Disappointed", int(row.get("disappointed") or 0)),
        ]
        return [
            {"label": label, "count": count, "percentage": _percentage(count, total)}
            for label, count in labels
        ]
    finally:
        cursor.close()
        conn.close()


def get_trend_summary(
    business_id,
    source=None,
    google_location_id=None,
    require_google_review_id=False,
):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    where_sql, params = _review_where_clause(
        business_id,
        source=source,
        google_location_id=google_location_id,
        require_google_review_id=require_google_review_id,
    )
    today = date.today()
    last_30_start = today - timedelta(days=30)
    previous_30_start = today - timedelta(days=60)
    current_month, previous_month = _month_boundaries()

    try:
        last_30 = _period_summary(cursor, where_sql, params, last_30_start, today + timedelta(days=1))
        previous_30 = _period_summary(cursor, where_sql, params, previous_30_start, last_30_start)
        this_month = _period_summary(cursor, where_sql, params, current_month, today + timedelta(days=1))
        last_month = _period_summary(cursor, where_sql, params, previous_month, current_month)
        topic_increase = _most_increased_negative_topic(
            cursor,
            business_id,
            source,
            google_location_id,
            require_google_review_id,
            last_30_start,
            today + timedelta(days=1),
            previous_30_start,
        )

        return {
            "last_30_days": _compare_periods(last_30, previous_30) | {
                "most_increased_negative_topic": topic_increase,
            },
            "this_month": _compare_periods(this_month, last_month),
        }
    finally:
        cursor.close()
        conn.close()


def get_latest_attention_reviews(
    business_id,
    source=None,
    google_location_id=None,
    limit=5,
    recent_days=30,
    require_google_review_id=False,
):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    where_sql, params = _review_where_clause(
        business_id,
        source=source,
        google_location_id=google_location_id,
        alias="r",
        require_google_review_id=require_google_review_id,
    )

    recent_sql = ""
    if recent_days:
        recent_sql = "AND COALESCE(r.review_created_at, r.review_date, r.created_at) >= DATE_SUB(NOW(), INTERVAL %s DAY)"
        params.append(recent_days)

    try:
        cursor.execute(
            f"""
            SELECT
                r.id,
                COALESCE(r.review_rating, r.rating) AS rating,
                r.review_text,
                r.sentiment,
                r.reply_status,
                COALESCE(r.review_created_at, r.review_date, r.created_at) AS review_date,
                (
                    SELECT rt.topic
                    FROM review_topics rt
                    WHERE rt.review_id = r.id
                    ORDER BY
                        CASE rt.sentiment WHEN 'negative' THEN 0 WHEN 'neutral' THEN 1 ELSE 2 END,
                        rt.confidence DESC
                    LIMIT 1
                ) AS topic
            FROM reviews r
            {where_sql}
            {recent_sql}
            AND (
                COALESCE(r.review_rating, r.rating) <= 2
                OR LOWER(COALESCE(r.sentiment, '')) = 'negative'
                OR r.reply_status = 'pending'
            )
            ORDER BY COALESCE(r.review_created_at, r.review_date, r.created_at) DESC
            LIMIT %s
            """,
            tuple(params + [limit])
        )
        rows = cursor.fetchall()
        return [
            {
                "id": row["id"],
                "rating": float(row["rating"] or 0),
                "review_text": (row["review_text"] or "")[:240],
                "sentiment": row.get("sentiment") or "",
                "reply_status": row.get("reply_status") or "",
                "review_date": str(row.get("review_date") or ""),
                "topic": row.get("topic") or "general experience",
                "suggested_action": _attention_action(row),
            }
            for row in rows
        ]
    finally:
        cursor.close()
        conn.close()


def get_latest_review_timestamp(
    business_id,
    source=None,
    google_location_id=None,
    require_google_review_id=False,
):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    where_sql, params = _review_where_clause(
        business_id,
        source=source,
        google_location_id=google_location_id,
        require_google_review_id=require_google_review_id,
    )

    try:
        cursor.execute(
            f"""
            SELECT MAX(COALESCE(review_updated_at, review_created_at, review_date, created_at)) AS latest_review_at
            FROM reviews
            {where_sql}
            """,
            tuple(params)
        )
        row = cursor.fetchone() or {}
        return row.get("latest_review_at")
    finally:
        cursor.close()
        conn.close()


def _recent_metrics(cursor, where_sql, params):
    start_date = date.today() - timedelta(days=30)
    cursor.execute(
        f"""
        SELECT
            COUNT(*) AS recent_review_count,
            AVG(COALESCE(review_rating, rating)) AS recent_average_rating
        FROM reviews
        {where_sql}
        AND COALESCE(review_created_at, review_date, created_at) >= %s
        """,
        tuple(params + [start_date])
    )
    row = cursor.fetchone() or {}
    return {
        "recent_review_count": int(row.get("recent_review_count") or 0),
        "recent_average_rating": round(_safe_float(row.get("recent_average_rating")) or 0, 2),
    }


def _attention_counts(cursor, where_sql, params):
    cursor.execute(
        f"""
        SELECT
            SUM(CASE
                WHEN COALESCE(review_rating, rating) <= 2
                OR LOWER(COALESCE(sentiment, '')) = 'negative'
                THEN 1 ELSE 0
            END) AS critical_negative_review_count,
            SUM(CASE
                WHEN (
                    COALESCE(review_rating, rating) <= 2
                    OR LOWER(COALESCE(sentiment, '')) = 'negative'
                )
                AND reply_status='pending'
                THEN 1 ELSE 0
            END) AS unanswered_negative_review_count
        FROM reviews
        {where_sql}
        """,
        tuple(params)
    )
    row = cursor.fetchone() or {}
    return {
        "critical_negative_review_count": int(row.get("critical_negative_review_count") or 0),
        "unanswered_negative_review_count": int(row.get("unanswered_negative_review_count") or 0),
    }


def _period_summary(cursor, where_sql, params, start_date, end_date):
    cursor.execute(
        f"""
        SELECT
            COUNT(*) AS review_count,
            AVG(COALESCE(review_rating, rating)) AS average_rating,
            SUM(CASE WHEN COALESCE(review_rating, rating) >= 4 THEN 1 ELSE 0 END) AS positive_count,
            SUM(CASE WHEN COALESCE(review_rating, rating) <= 2 THEN 1 ELSE 0 END) AS negative_count
        FROM reviews
        {where_sql}
        AND COALESCE(review_created_at, review_date, created_at) >= %s
        AND COALESCE(review_created_at, review_date, created_at) < %s
        """,
        tuple(params + [start_date, end_date])
    )
    row = cursor.fetchone() or {}
    return {
        "review_count": int(row.get("review_count") or 0),
        "average_rating": round(_safe_float(row.get("average_rating")) or 0, 2),
        "positive_count": int(row.get("positive_count") or 0),
        "negative_count": int(row.get("negative_count") or 0),
    }


def _compare_periods(current, previous):
    return {
        "current": current,
        "previous": previous,
        "rating_change": round((current["average_rating"] or 0) - (previous["average_rating"] or 0), 2),
        "review_volume_change": current["review_count"] - previous["review_count"],
        "negative_review_change": current["negative_count"] - previous["negative_count"],
        "positive_review_change": current["positive_count"] - previous["positive_count"],
    }


def _most_increased_negative_topic(
    cursor,
    business_id,
    source,
    google_location_id,
    require_google_review_id,
    current_start,
    current_end,
    previous_start,
):
    join_filter = []
    params = [current_start, current_end, previous_start, current_start, business_id]
    if source:
        join_filter.append("AND r.source=%s")
        params.append(source)
    if google_location_id:
        join_filter.append("AND r.google_location_id=%s")
        params.append(google_location_id)
    if require_google_review_id:
        join_filter.append("AND r.google_review_id IS NOT NULL")

    cursor.execute(
        f"""
        SELECT
            rt.topic,
            SUM(CASE WHEN COALESCE(r.review_created_at, r.review_date, r.created_at) >= %s
                AND COALESCE(r.review_created_at, r.review_date, r.created_at) < %s THEN 1 ELSE 0 END) AS current_count,
            SUM(CASE WHEN COALESCE(r.review_created_at, r.review_date, r.created_at) >= %s
                AND COALESCE(r.review_created_at, r.review_date, r.created_at) < %s THEN 1 ELSE 0 END) AS previous_count
        FROM review_topics rt
        JOIN reviews r
            ON r.id = rt.review_id
        WHERE rt.business_id=%s
        AND rt.sentiment='negative'
        {" ".join(join_filter)}
        GROUP BY rt.topic
        """,
        tuple(params)
    )
    best = None
    for row in cursor.fetchall():
        current_count = int(row.get("current_count") or 0)
        previous_count = int(row.get("previous_count") or 0)
        increase = current_count - previous_count
        if increase <= 0:
            continue
        percentage = round((increase / max(previous_count, 1)) * 100)
        candidate = {
            "topic": row["topic"],
            "current_count": current_count,
            "previous_count": previous_count,
            "increase": increase,
            "percentage_change": percentage,
        }
        if not best or candidate["increase"] > best["increase"]:
            best = candidate
    return best


def _attention_action(row):
    topic = row.get("topic") or "the issue"
    if row.get("reply_status") == "pending":
        return f"Reply today, acknowledge {topic}, and offer an offline resolution path."
    if float(row.get("rating") or 0) <= 2:
        return f"Review the {topic} complaint with the responsible team this week."
    return f"Monitor {topic} and use the reply generator if the review is not answered."


def _month_boundaries():
    today = date.today()
    current_start = today.replace(day=1)
    if current_start.month == 1:
        previous_start = current_start.replace(year=current_start.year - 1, month=12)
    else:
        previous_start = current_start.replace(month=current_start.month - 1)
    return current_start, previous_start


def _earliest_google_review_date(cursor, business_id, google_location_id=None):
    cursor.execute(
        """
        SELECT MIN(DATE(COALESCE(review_created_at, review_date, created_at))) AS first_review_date
        FROM reviews
        WHERE business_id=%s
        AND source='google'
        AND google_review_id IS NOT NULL
        AND (%s IS NULL OR google_location_id=%s)
        """,
        (business_id, google_location_id, google_location_id),
    )
    row = cursor.fetchone() or {}
    return _as_date(row.get("first_review_date"))


def _trend_granularity(duration_days):
    if duration_days <= 60:
        return "day"
    if duration_days <= 366:
        return "week"
    return "month"


def _trend_buckets(start_date, end_date, granularity):
    buckets = {}
    current = _bucket_start_for_date(start_date, start_date, granularity)

    while current <= end_date:
        buckets[current] = {
            "period_start": current,
            "review_count": 0,
            "cumulative_review_count": 0,
            "average_rating_for_period": None,
            "_rating_sum": 0.0,
            "_rating_count": 0,
        }
        current = _next_bucket_start(current, granularity)

    return buckets


def _bucket_start_for_date(value, start_date, granularity):
    value = _as_date(value) or start_date
    if granularity == "day":
        return value
    if granularity == "week":
        offset = (value - start_date).days
        return start_date + timedelta(days=(offset // 7) * 7)
    return value.replace(day=1)


def _next_bucket_start(value, granularity):
    if granularity == "day":
        return value + timedelta(days=1)
    if granularity == "week":
        return value + timedelta(days=7)
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1, day=1)
    return value.replace(month=value.month + 1, day=1)


def _period_label(period_start, index, granularity):
    if granularity == "day":
        return period_start.strftime("%d %b")
    if granularity == "week":
        return f"Week {index}"
    return period_start.strftime("%b %Y")


def _trend_insight(data):
    if not data:
        return (
            "No live Google reviews available yet. Connect Google Business Profile and sync reviews to view trends.",
            0,
            0,
            0,
        )

    current = int(data[-1].get("review_count") or 0)
    previous = int(data[-2].get("review_count") or 0) if len(data) > 1 else 0

    if previous == 0 and current > 0:
        return (
            "Review volume started this period with new live Google reviews.",
            current,
            previous,
            100,
        )
    if previous == 0:
        return (
            "Review volume is stable compared to the previous period.",
            current,
            previous,
            0,
        )

    change = round(((current - previous) / previous) * 100)
    if change > 5:
        message = f"Review volume increased by {change}% compared to the previous period."
    elif change < -5:
        message = f"Review volume decreased by {abs(change)}% compared to the previous period."
    else:
        message = "Review volume is stable compared to the previous period."
    return message, current, previous, change


def _as_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def _review_where_clause(
    business_id,
    source=None,
    google_location_id=None,
    alias=None,
    require_google_review_id=False,
):
    prefix = f"{alias}." if alias else ""
    clauses = [f"{prefix}business_id=%s"]
    params = [business_id]

    if source:
        clauses.append(f"{prefix}source=%s")
        params.append(source)

    if google_location_id:
        clauses.append(f"{prefix}google_location_id=%s")
        params.append(google_location_id)

    if require_google_review_id:
        clauses.append(f"{prefix}google_review_id IS NOT NULL")

    return "WHERE " + " AND ".join(clauses), params


def _percentage(count, total):
    return round((count / total) * 100, 1) if total else 0


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
