import json
import re
import google.generativeai as genai
from app.config import Config


genai.configure(api_key=Config.GEMINI_API_KEY)

model = genai.GenerativeModel("gemini-2.5-flash")


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
    "top_praises": [
        "...",
        "..."
    ],
    "top_complaints": [
        "...",
        "..."
    ],
    "recommendations": [
        "...",
        "...",
        "..."
    ],
    "sentiment_score": 0-100
}}

Reviews:

{combined_reviews}
"""

    response = model.generate_content(prompt)

    response_text = response.text.strip()

    match = re.search(
        r"\{.*\}",
        response_text,
        re.DOTALL
    )

    if not match:
        raise Exception(
            f"No JSON found in Gemini response: {response_text}"
        )

    return json.loads(match.group())
def analyze_review_and_save(
    cursor,
    review_id,
    review_text
):

    analysis = analyze_review(review_text)

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

def analyze_review(review_text):

    prompt = f"""
Analyze the following customer review.

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

    response = model.generate_content(prompt)

    response_text = response.text.strip()

    match = re.search(
        r"\{.*\}",
        response_text,
        re.DOTALL
    )

    if not match:
        raise Exception(
            f"No JSON found in Gemini response: {response_text}"
        )

    return json.loads(match.group())

def analyze_single_review(review_text):

    prompt = f"""
Analyze this customer review.

Return ONLY valid JSON.

Format:

{{
    "sentiment": "Positive | Neutral | Negative",
    "summary": "...",
    "reply": "..."
}}

Review:

{review_text}
"""

    response = model.generate_content(prompt)

    response_text = response.text.strip()

    match = re.search(
        r"\{.*\}",
        response_text,
        re.DOTALL
    )

    if not match:

        raise Exception(
            f"No JSON found in Gemini response: {response_text}"
        )

    return json.loads(match.group())