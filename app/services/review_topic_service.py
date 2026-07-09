import re

from flask import current_app

from app.services.ai_service import AIService, AIServiceError
from app.services.database_service import get_connection


TOPIC_KEYWORDS = {
    "service": ["service", "served", "care", "helpful", "rude", "response"],
    "staff": ["staff", "team", "employee", "manager", "reception", "front desk"],
    "food": ["food", "meal", "taste", "breakfast", "lunch", "dinner", "menu"],
    "pricing": ["price", "pricing", "cost", "expensive", "cheap", "value", "bill"],
    "cleanliness": ["clean", "dirty", "hygiene", "smell", "washroom", "bathroom"],
    "parking": ["parking", "park", "vehicle", "car"],
    "room": ["room", "bed", "stay", "hotel", "ac", "bathroom"],
    "location": ["location", "area", "near", "distance", "place", "view"],
    "delivery": ["delivery", "delivered", "shipping", "late order"],
    "waiting time": ["wait", "waiting", "delay", "slow", "queue", "late"],
    "ambience": ["ambience", "atmosphere", "music", "decor", "vibe"],
    "product quality": ["quality", "product", "item", "durable", "broken"],
    "customer support": ["support", "customer care", "call", "email", "complaint"],
}

POSITIVE_WORDS = {
    "good", "great", "excellent", "amazing", "best", "friendly", "helpful",
    "clean", "quick", "fast", "love", "liked", "perfect", "nice", "happy",
}
NEGATIVE_WORDS = {
    "bad", "poor", "worst", "rude", "dirty", "slow", "late", "delay",
    "expensive", "overpriced", "unhappy", "disappointed", "terrible", "issue",
}


def extract_topics_for_business(
    business_id,
    max_gemini_reviews=25,
    source=None,
    google_location_id=None,
    require_google_review_id=False,
):
    """Extracts and stores missing review topics for one business."""
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    filters = ["r.business_id=%s", "rt.id IS NULL"]
    params = [business_id]

    if source:
        filters.append("r.source=%s")
        params.append(source)

    if google_location_id:
        filters.append("r.google_location_id=%s")
        params.append(google_location_id)

    if require_google_review_id:
        filters.append("r.google_review_id IS NOT NULL")

    try:
        cursor.execute(
            f"""
            SELECT r.id, r.business_id, r.review_text, COALESCE(r.review_rating, r.rating) AS rating
            FROM reviews r
            LEFT JOIN review_topics rt
                ON rt.review_id = r.id
            WHERE {" AND ".join(filters)}
            ORDER BY COALESCE(r.review_created_at, r.review_date, r.created_at) DESC
            """,
            tuple(params)
        )
        reviews = cursor.fetchall()

        inserted = 0
        for index, review in enumerate(reviews):
            topics = _detect_topics(
                review,
                allow_gemini=index < max_gemini_reviews,
            )
            primary_topic = topics[0] if topics else _fallback_topic(review)
            for topic in topics:
                cursor.execute(
                    """
                    INSERT IGNORE INTO review_topics
                    (review_id, business_id, topic, sentiment, confidence)
                    VALUES (%s,%s,%s,%s,%s)
                    """,
                    (
                        review["id"],
                        review["business_id"],
                        topic["topic"],
                        topic["sentiment"],
                        topic["confidence"],
                    )
                )
                inserted += cursor.rowcount

            cursor.execute(
                """
                UPDATE reviews
                SET
                    sentiment=COALESCE(NULLIF(sentiment, ''), %s),
                    category=COALESCE(NULLIF(category, ''), %s)
                WHERE id=%s
                """,
                (
                    primary_topic["sentiment"].title(),
                    primary_topic["topic"],
                    review["id"],
                )
            )

        conn.commit()
        return {"processed_reviews": len(reviews), "inserted_topics": inserted}
    finally:
        cursor.close()
        conn.close()


def _detect_topics(review, allow_gemini=True):
    text = (review.get("review_text") or "").strip()
    if not text:
        return [_fallback_topic(review)]

    if allow_gemini:
        try:
            topics = _detect_topics_with_gemini(review)
            if topics:
                return topics
        except Exception as error:
            current_app.logger.warning(
                "Gemini topic extraction failed for review_id=%s: %s",
                review.get("id"),
                error,
            )

    return _detect_topics_with_keywords(review)


def _detect_topics_with_gemini(review):
    prompt = f"""
Classify this customer review into up to 3 practical business topics.

Allowed topics:
{", ".join(TOPIC_KEYWORDS.keys())}, other

Return ONLY valid JSON:
{{
  "topics": [
    {{"topic": "service", "sentiment": "positive", "confidence": 0.82}}
  ]
}}

Rules:
- sentiment must be one of positive, neutral, negative.
- confidence must be between 0 and 1.
- Prefer specific operational topics over "other".
- Do not invent topics not supported by the review.

Review rating: {review.get("rating") or ""}
Review text: {review.get("review_text") or ""}
"""
    result = AIService().generate_json(prompt, "review_topic_extraction", max_retries=1)
    rows = result.data.get("topics", [])
    normalized = []
    for row in rows[:3]:
        topic = _clean_topic(row.get("topic"))
        if not topic:
            continue
        sentiment = _clean_sentiment(row.get("sentiment"))
        confidence = _safe_confidence(row.get("confidence"), 0.75)
        normalized.append(
            {"topic": topic, "sentiment": sentiment, "confidence": confidence}
        )
    return _dedupe_topics(normalized)


def _detect_topics_with_keywords(review):
    text = (review.get("review_text") or "").lower()
    matched = []

    for topic, keywords in TOPIC_KEYWORDS.items():
        score = sum(1 for keyword in keywords if keyword in text)
        if score:
            matched.append((topic, score))

    matched.sort(key=lambda item: item[1], reverse=True)
    sentiment = _fallback_sentiment(review)

    if not matched:
        return [_fallback_topic(review)]

    return [
        {
            "topic": topic,
            "sentiment": sentiment,
            "confidence": min(0.95, 0.58 + (score * 0.1)),
        }
        for topic, score in matched[:3]
    ]


def _fallback_topic(review):
    return {
        "topic": "other",
        "sentiment": _fallback_sentiment(review),
        "confidence": 0.45,
    }


def _fallback_sentiment(review):
    rating = _safe_float(review.get("rating"))
    if rating is not None:
        if rating >= 4:
            return "positive"
        if rating <= 2:
            return "negative"

    words = set(re.findall(r"[a-z]+", (review.get("review_text") or "").lower()))
    positive_hits = len(words & POSITIVE_WORDS)
    negative_hits = len(words & NEGATIVE_WORDS)

    if positive_hits > negative_hits:
        return "positive"
    if negative_hits > positive_hits:
        return "negative"
    return "neutral"


def _clean_topic(value):
    topic = str(value or "").strip().lower()
    if topic in TOPIC_KEYWORDS or topic == "other":
        return topic
    return "other" if topic else None


def _clean_sentiment(value):
    sentiment = str(value or "").strip().lower()
    return sentiment if sentiment in {"positive", "neutral", "negative"} else "neutral"


def _safe_confidence(value, fallback):
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return fallback


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dedupe_topics(rows):
    seen = set()
    deduped = []
    for row in rows:
        key = row["topic"]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped
