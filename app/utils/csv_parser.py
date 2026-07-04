import math
import pandas as pd


def _is_missing(value):
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if pd.isna(value):
        return True
    return False


def _normalize_column_name(column_name):
    if column_name is None:
        return None
    return str(column_name).strip().lower().replace(" ", "_").replace("-", "_")


def get_csv_value(row, column_names, default=None):
    normalized_row = {
        _normalize_column_name(key): value
        for key, value in row.items()
    }

    for column_name in column_names:
        normalized_name = _normalize_column_name(column_name)
        if normalized_name in normalized_row:
            value = normalized_row[normalized_name]
            if not _is_missing(value):
                if isinstance(value, str):
                    value = value.strip()
                return value
    return default


def normalize_csv_row(row):
    normalized = {}

    source = get_csv_value(row, ["source", "platform", "review_source"], default="excel")
    rating = get_csv_value(row, ["rating", "stars", "score"], default=None)
    review_title = get_csv_value(row, ["review_title", "title"], default="")
    review_text = get_csv_value(row, ["review_text", "text", "comment"], default="")
    reviewer_name = get_csv_value(row, ["reviewer_name", "customer_name", "name"], default="Anonymous")
    review_date = get_csv_value(row, ["review_date", "date", "reviewed_at"], default=None)

    if not _is_missing(rating):
        try:
            normalized_rating = float(rating)
        except (TypeError, ValueError):
            normalized_rating = None
        normalized["rating"] = normalized_rating
    else:
        normalized["rating"] = None

    if not _is_missing(review_date):
        try:
            normalized_date = pd.to_datetime(review_date, dayfirst=True, errors="coerce")
            if pd.isna(normalized_date):
                normalized["review_date"] = None
            else:
                normalized["review_date"] = normalized_date.strftime("%Y-%m-%d")
        except Exception:
            normalized["review_date"] = None
    else:
        normalized["review_date"] = None

    normalized["source"] = source
    normalized["review_title"] = review_title or ""
    normalized["review_text"] = review_text or ""
    normalized["reviewer_name"] = reviewer_name or "Anonymous"

    return normalized
