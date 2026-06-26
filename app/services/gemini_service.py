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