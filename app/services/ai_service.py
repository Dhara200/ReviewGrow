import json
import re
import time
from dataclasses import dataclass

import google.generativeai as genai

from app.config import Config


DEFAULT_PROVIDER = "gemini"
DEFAULT_MODEL = "gemini-2.5-flash"


class AIServiceError(Exception):
    def __init__(self, message, retryable=False, result=None):
        super().__init__(message)
        self.retryable = retryable
        self.result = result


@dataclass
class AIResult:
    data: dict
    provider: str
    model_name: str
    operation_type: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost: float
    request_status: str
    response_time_ms: int
    error_message: str | None = None
    attempt_logs: list | None = None


def _extract_json(response_text):
    response_text = (response_text or "").strip()
    response_text = (
        response_text
        .replace("```json", "")
        .replace("```", "")
        .strip()
    )

    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"(\{.*\}|\[.*\])", response_text, re.DOTALL)
    if not match:
        raise AIServiceError(
            f"No JSON found in AI response: {response_text}",
            retryable=False
        )

    return json.loads(match.group())


def _is_retryable_error(error):
    text = str(error).lower()
    retryable_markers = [
        "429",
        "rate limit",
        "quota",
        "timeout",
        "temporarily",
        "unavailable",
        "internal",
        "500",
        "502",
        "503",
        "504",
    ]
    return any(marker in text for marker in retryable_markers)


def _usage_value(usage, *names):
    for name in names:
        value = getattr(usage, name, None)
        if value is not None:
            return int(value or 0)
    return 0


def _estimate_cost(provider, model_name, input_tokens, output_tokens):
    # Placeholder pricing for quota and billing readiness. Replace from config
    # when final provider pricing is chosen.
    if provider == "gemini" and model_name == "gemini-2.5-flash":
        input_per_million = float(getattr(Config, "GEMINI_FLASH_INPUT_COST_PER_1M", 0.30))
        output_per_million = float(getattr(Config, "GEMINI_FLASH_OUTPUT_COST_PER_1M", 2.50))
        return round(
            (input_tokens / 1_000_000 * input_per_million)
            + (output_tokens / 1_000_000 * output_per_million),
            6
        )

    return 0.0


class AIService:
    def __init__(self, provider=None, model_name=None):
        self.provider = provider or getattr(Config, "AI_PROVIDER", DEFAULT_PROVIDER)
        self.model_name = model_name or getattr(Config, "AI_MODEL_NAME", DEFAULT_MODEL)

        if self.provider != "gemini":
            raise ValueError(f"Unsupported AI provider: {self.provider}")

        genai.configure(api_key=Config.GEMINI_API_KEY)
        self.model = genai.GenerativeModel(self.model_name)

    def analyze_review_batch(self, reviews):
        review_payload = [
            {
                "review_id": review["id"],
                "rating": str(review.get("rating") or ""),
                "source": review.get("source") or "",
                "review_text": review.get("review_text") or "",
            }
            for review in reviews
        ]

        prompt = f"""
Analyze the following customer reviews.

Return ONLY valid JSON in this exact shape:
{{
  "reviews": [
    {{
      "review_id": 123,
      "sentiment": "Positive | Neutral | Negative",
      "category": "service | product | pricing | staff | delivery | cleanliness | other",
      "theme": "short complaint or praise theme",
      "summary": "one sentence review insight",
      "suggested_reply": "professional business reply",
      "confidence_score": 0.0
    }}
  ]
}}

Rules:
- Include one result for each input review_id.
- confidence_score must be between 0 and 1.
- Do not invent customer details.

Reviews:
{json.dumps(review_payload, ensure_ascii=False)}
"""
        result = self.generate_json(prompt, "review_batch_analysis")
        rows = result.data.get("reviews", [])
        if not isinstance(rows, list):
            rows = []
        result.data = {"reviews": rows}
        return result

    def generate_business_report(self, analyzed_reviews):
        review_payload = [
            {
                "sentiment": row.get("sentiment"),
                "category": row.get("category"),
                "theme": row.get("complaint_praise_theme") or row.get("theme"),
                "summary": row.get("summary"),
                "rating": str(row.get("rating") or ""),
            }
            for row in analyzed_reviews
        ]

        prompt = f"""
Create a business-level reputation report from these analyzed reviews.

Return ONLY valid JSON:
{{
  "summary": "...",
  "top_praises": ["..."],
  "top_complaints": ["..."],
  "recommendations": ["..."],
  "sentiment_score": 85
}}

Reviews:
{json.dumps(review_payload, ensure_ascii=False)}
"""
        result = self.generate_json(prompt, "business_report")
        data = result.data
        result.data = {
            "summary": data.get("summary", ""),
            "top_praises": data.get("top_praises", []),
            "top_complaints": data.get("top_complaints", []),
            "recommendations": data.get("recommendations", []),
            "sentiment_score": data.get("sentiment_score", 0),
        }
        return result

    def generate_json(self, prompt, operation_type, max_retries=3, base_delay=2):
        last_error = None
        attempt_logs = []

        for attempt in range(max_retries + 1):
            started = time.monotonic()
            try:
                response = self.model.generate_content(prompt)
                response_time_ms = int((time.monotonic() - started) * 1000)
                usage = getattr(response, "usage_metadata", None)
                input_tokens = _usage_value(usage, "prompt_token_count")
                output_tokens = _usage_value(usage, "candidates_token_count")
                total_tokens = _usage_value(usage, "total_token_count") or input_tokens + output_tokens

                return AIResult(
                    data=_extract_json(response.text),
                    provider=self.provider,
                    model_name=self.model_name,
                    operation_type=operation_type,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                    estimated_cost=_estimate_cost(
                        self.provider,
                        self.model_name,
                        input_tokens,
                        output_tokens
                    ),
                    request_status="success",
                    response_time_ms=response_time_ms,
                    attempt_logs=attempt_logs,
                )
            except Exception as error:
                response_time_ms = int((time.monotonic() - started) * 1000)
                retryable = _is_retryable_error(error)
                last_error = AIResult(
                    data={},
                    provider=self.provider,
                    model_name=self.model_name,
                    operation_type=operation_type,
                    input_tokens=0,
                    output_tokens=0,
                    total_tokens=0,
                    estimated_cost=0.0,
                    request_status="failed",
                    response_time_ms=response_time_ms,
                    error_message=str(error)[:1000],
                )
                attempt_logs.append(last_error)

                if not retryable or attempt >= max_retries:
                    last_error.attempt_logs = attempt_logs[:-1]
                    raise AIServiceError(
                        str(error),
                        retryable=retryable,
                        result=last_error
                    ) from error

                time.sleep(base_delay * (2 ** attempt))

        raise AIServiceError(
            last_error.error_message if last_error else "AI request failed",
            retryable=True,
            result=last_error
        )


def log_ai_usage(cursor, user_id, business_id, result):
    for attempt in result.attempt_logs or []:
        log_ai_usage(cursor, user_id, business_id, attempt)

    cursor.execute(
        """
        INSERT INTO ai_usage_logs
        (
            user_id,
            business_id,
            provider,
            model_name,
            operation_type,
            input_tokens,
            output_tokens,
            total_tokens,
            estimated_cost,
            request_status,
            response_time_ms,
            error_message
        )
        VALUES
        (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            user_id,
            business_id,
            result.provider,
            result.model_name,
            result.operation_type,
            result.input_tokens,
            result.output_tokens,
            result.total_tokens,
            result.estimated_cost,
            result.request_status,
            result.response_time_ms,
            result.error_message,
        )
    )
