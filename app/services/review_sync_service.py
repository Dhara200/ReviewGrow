from datetime import datetime

from app.services.google_business_service import GoogleBusinessError, list_reviews


STAR_RATING_MAP = {
    "ONE": 1,
    "TWO": 2,
    "THREE": 3,
    "FOUR": 4,
    "FIVE": 5
}


def _parse_google_datetime(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _review_text(review):
    comment = (review.get("comment") or "").strip()

    if comment:
        return comment

    rating = review.get("starRating") or "UNKNOWN"

    return f"Google rating-only review: {rating.title()}"


def _reviewer_name(review):
    reviewer = review.get("reviewer") or {}
    return (reviewer.get("displayName") or "").strip()


def _rating_value(review):
    value = review.get("starRating")
    return STAR_RATING_MAP.get(value)


def _google_review_id(review):
    review_id = review.get("reviewId")

    if review_id:
        return review_id

    name = review.get("name") or ""
    return name.rstrip("/").split("/")[-1] if name else None


def sync_google_reviews(cursor, connection, allow_internal_api_retry=True):
    if not connection.get("google_account_id") or not connection.get("google_location_id"):
        raise GoogleBusinessError(
            "Google Business Profile location is not selected. Please select a location before syncing reviews."
        )

    google_location_id = connection["google_location_id"]
    google_reviews = list_reviews(
        connection["access_token"],
        connection["google_account_id"],
        google_location_id,
        allow_internal_retry=allow_internal_api_retry,
    )

    inserted_count = 0
    updated_count = 0

    for review in google_reviews:
        google_review_id = _google_review_id(review)
        external_review_id = google_review_id

        if not google_review_id:
            continue

        rating = _rating_value(review)
        text = _review_text(review)
        reviewer_name = _reviewer_name(review)
        create_time = _parse_google_datetime(review.get("createTime"))
        update_time = _parse_google_datetime(review.get("updateTime"))

        cursor.execute(
            """
            SELECT id, review_text, rating, review_updated_at
            FROM reviews
            WHERE business_id=%s
            AND (
                google_review_id=%s
                OR (
                    google_review_id IS NULL
                    AND external_review_id=%s
                    AND google_location_id=%s
                )
            )
            """,
            (
                connection["business_id"],
                google_review_id,
                external_review_id,
                google_location_id
            )
        )

        existing = cursor.fetchone()

        if existing:
            text_changed = (existing.get("review_text") or "") != (text or "")
            rating_changed = str(existing.get("rating") or "") != str(rating or "")

            if text_changed:
                cursor.execute(
                    """
                    UPDATE reviews
                    SET
                        rating=%s,
                        review_rating=%s,
                        review_text=%s,
                        reviewer_name=%s,
                        review_date=%s,
                        review_created_at=%s,
                        review_updated_at=%s,
                        external_review_id=%s,
                        google_review_id=%s,
                        google_location_id=%s,
                        source='google',
                        source_platform='google',
                        analysis_status='pending',
                        suggested_reply=NULL,
                        ai_reply=NULL,
                        reply_status='pending',
                        reply_generated_at=NULL,
                        reply_posted_at=NULL,
                        reply_error_message=NULL
                    WHERE id=%s
                    """,
                    (
                        rating,
                        rating,
                        text,
                        reviewer_name,
                        create_time,
                        create_time,
                        update_time,
                        external_review_id,
                        google_review_id,
                        google_location_id,
                        existing["id"]
                    )
                )
                updated_count += 1
            elif rating_changed:
                cursor.execute(
                    """
                    UPDATE reviews
                    SET
                        rating=%s,
                        review_rating=%s,
                        reviewer_name=%s,
                        review_date=%s,
                        review_created_at=%s,
                        review_updated_at=%s,
                        external_review_id=%s,
                        google_review_id=%s,
                        google_location_id=%s,
                        source='google',
                        source_platform='google'
                    WHERE id=%s
                    """,
                    (
                        rating,
                        rating,
                        reviewer_name,
                        create_time,
                        create_time,
                        update_time,
                        external_review_id,
                        google_review_id,
                        google_location_id,
                        existing["id"]
                    )
                )
                updated_count += 1
            else:
                cursor.execute(
                    """
                    UPDATE reviews
                    SET
                        reviewer_name=%s,
                        review_date=%s,
                        review_created_at=%s,
                        review_updated_at=%s,
                        external_review_id=%s,
                        google_review_id=%s,
                        google_location_id=%s,
                        source='google',
                        source_platform='google'
                    WHERE id=%s
                    """,
                    (
                        reviewer_name,
                        create_time,
                        create_time,
                        update_time,
                        external_review_id,
                        google_review_id,
                        google_location_id,
                        existing["id"]
                    )
                )
            review_id = existing["id"]
        else:
            cursor.execute(
                """
                INSERT INTO reviews
                (
                    business_id,
                    source,
                    rating,
                    review_rating,
                    review_title,
                    review_text,
                    reviewer_name,
                    review_date,
                    review_created_at,
                    review_updated_at,
                    external_review_id,
                    google_review_id,
                    google_location_id,
                    source_platform,
                    reply_status,
                    analysis_status
                )
                VALUES
                (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    connection["business_id"],
                    "google",
                    rating,
                    rating,
                    "Google Review",
                    text,
                    reviewer_name,
                    create_time,
                    create_time,
                    update_time,
                    external_review_id,
                    google_review_id,
                    google_location_id,
                    "google",
                    "pending",
                    "pending"
                )
            )
            review_id = cursor.lastrowid
            inserted_count += 1

    return {
        "fetched_count": len(google_reviews),
        "inserted_count": inserted_count,
        "updated_count": updated_count,
        "analyzed_count": 0
    }
