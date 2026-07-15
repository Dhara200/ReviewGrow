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


def sync_google_reviews(cursor, connection):
    if not connection.get("google_account_id") or not connection.get("google_location_id"):
        raise GoogleBusinessError(
            "Google Business Profile location is not selected. Please select a location before syncing reviews."
        )

    google_location_id = connection["google_location_id"]
    google_reviews = list_reviews(
        connection["access_token"],
        connection["google_account_id"],
        google_location_id
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
            INSERT INTO reviews
            (
                business_id, source, rating, review_rating, review_title,
                review_text, reviewer_name, review_date, review_created_at,
                review_updated_at, external_review_id, google_review_id,
                google_location_id, source_platform, reply_status, analysis_status
            )
            VALUES (%s,'google',%s,%s,'Google Review',%s,%s,%s,%s,%s,%s,%s,%s,'google','pending','pending')
            ON DUPLICATE KEY UPDATE
                id=LAST_INSERT_ID(id),
                analysis_status=IF(NOT (review_text <=> VALUES(review_text)), 'pending', analysis_status),
                analysis_error=IF(NOT (review_text <=> VALUES(review_text)), NULL, analysis_error),
                suggested_reply=IF(NOT (review_text <=> VALUES(review_text)), NULL, suggested_reply),
                ai_reply=IF(NOT (review_text <=> VALUES(review_text)), NULL, ai_reply),
                reply_status=IF(NOT (review_text <=> VALUES(review_text)), 'pending', reply_status),
                reply_generated_at=IF(NOT (review_text <=> VALUES(review_text)), NULL, reply_generated_at),
                reply_posted_at=IF(NOT (review_text <=> VALUES(review_text)), NULL, reply_posted_at),
                reply_error_message=IF(NOT (review_text <=> VALUES(review_text)), NULL, reply_error_message),
                rating=VALUES(rating), review_rating=VALUES(review_rating),
                review_text=VALUES(review_text), reviewer_name=VALUES(reviewer_name),
                review_date=VALUES(review_date), review_created_at=VALUES(review_created_at),
                review_updated_at=VALUES(review_updated_at), external_review_id=VALUES(external_review_id),
                source='google', source_platform='google'
            """,
            (
                connection["business_id"], rating, rating, text, reviewer_name,
                create_time, create_time, update_time, external_review_id,
                google_review_id, google_location_id,
            ),
        )
        if cursor.rowcount == 1:
            inserted_count += 1
        elif cursor.rowcount == 2:
            updated_count += 1

    return {
        "fetched_count": len(google_reviews),
        "inserted_count": inserted_count,
        "updated_count": updated_count,
        "analyzed_count": 0
    }
