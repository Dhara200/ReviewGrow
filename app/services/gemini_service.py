from app.services.ai_service import AIService, _extract_json


_ai_service = AIService()
model = _ai_service.model


def analyze_reviews(review_texts):

    combined_reviews = "\n".join(review_texts)

    prompt = f"""
Analyze these customer reviews.

Provide:

1. Summary
2. Top praises
3. Top complaints
4. Business improvement recommendations
5. Sentiment score

Return ONLY valid JSON.

Format:

{{
    "summary": "...",
    "top_praises": ["..."],
    "top_complaints": ["..."],
    "recommendations": ["..."],
    "sentiment_score": 85
}}

Reviews:

{combined_reviews}
"""

    result = _ai_service.generate_json(prompt, "business_report")
    data = result.data

    return {
        "summary": data.get("summary", ""),
        "top_praises": data.get("top_praises", []),
        "top_complaints": data.get("top_complaints", []),
        "recommendations": data.get("recommendations", []),
        "sentiment_score": data.get("sentiment_score", 0)
    }

def analyze_review_and_save(
    cursor,
    review_id,
    review_text
):

    analysis = analyze_single_review(review_text)

    cursor.execute(
        """
        UPDATE reviews
        SET
            sentiment=%s,
            summary=%s,
            ai_reply=%s,
            analysis_status='analyzed',
            analyzed_at=NOW()
        WHERE id=%s
        """,
        (
            analysis["sentiment"],
            analysis["summary"],
            analysis["ai_reply"],
            review_id
        )
    )

    return analysis

def analyze_single_review(review_text):

    prompt = f"""
Analyze this customer review.

Return ONLY valid JSON.

Format:

{{
    "summary": "...",
    "sentiment": "Positive | Neutral | Negative",
    "ai_reply": "..."
}}

Review:

{review_text}
"""

    result = _ai_service.generate_json(prompt, "single_review_analysis")
    data = result.data

    return {
        "summary": data.get("summary", ""),
        "sentiment": data.get("sentiment", "Neutral"),
        "ai_reply": data.get("ai_reply", "")
    }
