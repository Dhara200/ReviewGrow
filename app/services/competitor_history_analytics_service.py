from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.config import Config
from app.services.database_service import get_connection


ALLOWED_WINDOWS = ("available", "7", "30", "90")
WINDOW_DAYS = {"7": 7, "30": 30, "90": 90}
MAX_AVAILABLE_DAYS = 365
RATING_STABLE_THRESHOLD = Decimal("0.05")


def build_competitor_history(business_id, requested_window="available"):
    """Build one business-scoped historical view model without external calls."""
    requested = requested_window if requested_window in ALLOWED_WINDOWS else "available"
    connection = get_connection(); cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """SELECT id,competitor_name FROM business_competitors
               WHERE business_id=%s AND is_active=TRUE ORDER BY id""", (business_id,)
        )
        competitors = cursor.fetchall()
        cursor.execute(
            """SELECT s.capture_date,s.subject_type,s.subject_key,s.competitor_id,
                      s.subject_name,s.rating,s.user_rating_count,s.business_status,
                      s.refresh_job_id
               FROM business_reputation_snapshots s
               LEFT JOIN business_competitors c ON c.id=s.competitor_id
               WHERE s.business_id=%s
                 AND s.capture_date>=DATE_SUB(UTC_DATE(),INTERVAL %s DAY)
                 AND (s.subject_type='customer' OR
                      (c.business_id=%s AND c.is_active=TRUE))
               ORDER BY s.capture_date,s.subject_key""",
            (business_id, MAX_AVAILABLE_DAYS, business_id),
        )
        rows = cursor.fetchall()
    finally:
        cursor.close(); connection.close()
    return build_history_from_rows(rows, competitors, requested)


def build_history_from_rows(rows, active_competitors, requested_window="available", now=None):
    requested = requested_window if requested_window in ALLOWED_WINDOWS else "available"
    competitor_keys = [f"competitor:{int(item['id'])}" for item in active_competitors]
    allowed_keys = {"customer", *competitor_keys}
    values = {}
    names = {f"competitor:{int(item['id'])}": item.get("competitor_name") or "Competitor" for item in active_competitors}
    refresh_by_date = {}
    for raw in rows:
        key = str(raw.get("subject_key") or "")
        day = _as_date(raw.get("capture_date"))
        if key not in allowed_keys or day is None:
            continue
        names[key] = raw.get("subject_name") or names.get(key) or ("Your business" if key == "customer" else "Competitor")
        values.setdefault(key, {})[day] = {
            "rating": _decimal(raw.get("rating")),
            "review_count": _integer(raw.get("user_rating_count")),
            "business_status": raw.get("business_status"),
        }
        refresh_by_date.setdefault(day, set()).add(key)

    customer_dates = set(values.get("customer", {}))
    comparison_dates = sorted(day for day in customer_dates if any(day in values.get(key, {}) for key in competitor_keys))
    first_all, latest_all = (comparison_dates[0], comparison_dates[-1]) if comparison_dates else (None, None)
    full_span = (latest_all - first_all).days if first_all and latest_all else 0
    valid_count = len(comparison_dates)
    enabled = {
        "available": valid_count >= 2,
        "7": valid_count >= 7 and full_span >= 6,
        "30": valid_count >= 27 and full_span >= 29,
        "90": valid_count >= 81 and full_span >= 89,
    }
    selected = requested if requested == "available" or enabled.get(requested) else "available"
    selected_dates = _select_dates(comparison_dates, selected)
    axis = _continuous_axis(selected_dates)
    stage = 0 if valid_count == 0 else 1 if valid_count == 1 else 2 if valid_count < 7 else 3 if valid_count < 30 else 4

    subject_rows = []
    chart_subjects = []
    for key in ["customer", *competitor_keys]:
        history = values.get(key, {})
        metrics = _subject_metrics(key, names.get(key, "Business"), history, axis)
        subject_rows.append(metrics)
        chart_subjects.append({
            "subject_key": key, "name": metrics["name"], "is_customer": key == "customer",
            "rating": [_number(history.get(day, {}).get("rating")) for day in axis],
            "review_count": [history.get(day, {}).get("review_count") for day in axis],
        })

    averages, contributors = _average_series(values, competitor_keys, axis)
    ranks, rank_counts = _rank_series(values, competitor_keys, axis)
    current_ranks = _current_rank_map(values, competitor_keys, axis[-1] if axis else None)
    customer = next((item for item in subject_rows if item["subject_key"] == "customer"), _empty_subject("customer", "Your business"))
    for item in subject_rows:
        item["current_rank"] = current_ranks.get(item["subject_key"])
    summary = _summary(customer, values.get("customer", {}), averages, ranks, axis)
    leaders = _leaders(subject_rows, customer)
    quality = _data_quality(values, competitor_keys, axis, refresh_by_date, contributors, now)
    quality["selected_window_span"] = (axis[-1] - axis[0]).days if len(axis) >= 2 else 0
    insights = _insights(summary, leaders, quality, stage)

    actual_first, actual_latest = (axis[0], axis[-1]) if axis else (None, None)
    readiness = {
        "stage": stage, "valid_snapshot_dates": valid_count,
        "first_snapshot_date": first_all, "latest_snapshot_date": latest_all,
        "history_days": full_span, "weekly_unlocked": stage >= 3,
        "monthly_unlocked": stage >= 4,
        "weekly_remaining": max(0, 7 - valid_count),
        "monthly_remaining": max(0, 30 - valid_count),
        "next_expected_refresh": (now or datetime.now(timezone.utc)) + timedelta(hours=Config.COMPETITOR_REFRESH_HOURS),
    }
    windows = [
        {"value": key, "label": "Available history" if key == "available" else f"Last {key} days", "enabled": key == "available" or enabled[key]}
        for key in ALLOWED_WINDOWS
    ]
    return {
        "requested_window": requested, "selected_window": selected, "windows": windows,
        "readiness": readiness, "summary": summary, "subjects": subject_rows,
        "rating_series": {"labels": [day.isoformat() for day in axis], "subjects": chart_subjects, "competitor_average": [_number(point["rating"]) for point in averages]},
        "review_count_series": {"labels": [day.isoformat() for day in axis], "subjects": chart_subjects, "competitor_average": [_number(point["review_count"]) for point in averages]},
        "rank_series": {"labels": [day.isoformat() for day in axis], "customer": ranks, "competitor_count": rank_counts},
        "leaders": leaders, "insights": insights, "data_quality": quality,
        "chart_config": {"available": stage >= 3, "modes": ["rating", "review_count", "rank"] if stage >= 3 else []},
        "period": {
            "baseline_date": actual_first, "current_date": actual_latest,
            "span_days": (actual_latest - actual_first).days if actual_first and actual_latest else 0,
            "missing_days": max(0, len(axis) - len(selected_dates)),
            "coverage_percentage": round(len(selected_dates) / len(axis) * 100, 1) if axis else 0,
        },
    }


def rating_gap_interpretation(baseline_gap, current_gap):
    if baseline_gap is None or current_gap is None:
        return "Rating-gap comparison is unavailable."
    movement = current_gap - baseline_gap
    if abs(movement) < RATING_STABLE_THRESHOLD:
        return "The rating gap remained stable."
    if current_gap >= 0 and movement > 0:
        return f"Your rating lead increased by {abs(movement):.1f}."
    if baseline_gap < 0 and movement > 0:
        return f"The rating gap narrowed by {abs(movement):.1f}."
    if current_gap < 0 and movement < 0:
        return f"The rating gap widened by {abs(movement):.1f}."
    return f"Your rating lead narrowed by {abs(movement):.1f}."


def _select_dates(dates, window):
    if not dates or window == "available": return dates
    cutoff = dates[-1] - timedelta(days=WINDOW_DAYS[window] - 1)
    return [day for day in dates if day >= cutoff]


def _continuous_axis(dates):
    if not dates: return []
    return [dates[0] + timedelta(days=offset) for offset in range((dates[-1] - dates[0]).days + 1)]


def _subject_metrics(key, name, history, axis):
    rating_points = [(day, history[day]["rating"]) for day in axis if day in history and history[day]["rating"] is not None]
    count_points = [(day, history[day]["review_count"]) for day in axis if day in history and history[day]["review_count"] is not None]
    rating_change = rating_points[-1][1] - rating_points[0][1] if len(rating_points) >= 2 and rating_points[0][0] != rating_points[-1][0] else None
    review_growth = count_points[-1][1] - count_points[0][1] if len(count_points) >= 2 and count_points[0][0] != count_points[-1][0] else None
    span = (max(history) - min(history)).days if len(history) >= 2 else 0
    return {
        "subject_key": key, "name": name, "is_customer": key == "customer",
        "baseline_rating": _number(rating_points[0][1]) if rating_points else None,
        "current_rating": _number(rating_points[-1][1]) if rating_points else None,
        "rating_change": _number(rating_change),
        "baseline_review_count": count_points[0][1] if count_points else None,
        "current_review_count": count_points[-1][1] if count_points else None,
        "review_growth": review_growth,
        "review_growth_percentage": round(review_growth / count_points[0][1] * 100, 1) if review_growth is not None and count_points[0][1] >= 10 else None,
        "reviews_per_day": round(review_growth / span, 2) if review_growth is not None and span > 0 and review_growth >= 0 else None,
        "history_span": span, "snapshot_count": len(history),
        "coverage_percentage": round(sum(day in history for day in axis) / len(axis) * 100, 1) if axis else 0,
        "rating_status": _movement_label(rating_change), "review_count_anomaly": review_growth is not None and review_growth < 0,
    }


def _average_series(values, competitor_keys, axis):
    points, contributors = [], []
    for day in axis:
        ratings = [values[key][day]["rating"] for key in competitor_keys if day in values.get(key, {}) and values[key][day]["rating"] is not None]
        counts = [values[key][day]["review_count"] for key in competitor_keys if day in values.get(key, {}) and values[key][day]["review_count"] is not None]
        points.append({"rating": sum(ratings) / len(ratings) if ratings else None, "review_count": Decimal(sum(counts)) / len(counts) if counts else None})
        contributors.append(max(len(ratings), len(counts)))
    return points, contributors


def _rank_series(values, competitor_keys, axis):
    ranks, counts = [], []
    for day in axis:
        candidates = []
        for key in ["customer", *competitor_keys]:
            point = values.get(key, {}).get(day)
            if point and point["rating"] is not None:
                candidates.append((key, point["rating"], point["review_count"] if point["review_count"] is not None else -1))
        candidates.sort(key=lambda item: (-item[1], -item[2], item[0]))
        ranks.append(next((index + 1 for index, item in enumerate(candidates) if item[0] == "customer"), None))
        counts.append(len(candidates))
    return ranks, counts


def _current_rank_map(values, competitor_keys, day):
    if day is None: return {}
    candidates = []
    for key in ["customer", *competitor_keys]:
        point = values.get(key, {}).get(day)
        if point and point["rating"] is not None:
            candidates.append((key, point["rating"], point["review_count"] if point["review_count"] is not None else -1))
    candidates.sort(key=lambda item: (-item[1], -item[2], item[0]))
    return {item[0]: index + 1 for index, item in enumerate(candidates)}


def _summary(customer, customer_history, averages, ranks, axis):
    valid_indices = [index for index, day in enumerate(axis) if day in customer_history and averages[index]["rating"] is not None]
    baseline_index, current_index = (valid_indices[0], valid_indices[-1]) if len(valid_indices) >= 2 else (None, None)
    def gap(index, field):
        if index is None: return None
        own = customer_history.get(axis[index], {}).get("rating" if field == "rating" else "review_count")
        average = averages[index][field]
        return own - average if own is not None and average is not None else None
    rating_baseline, rating_current = gap(baseline_index, "rating"), gap(current_index, "rating")
    count_baseline, count_current = gap(baseline_index, "review_count"), gap(current_index, "review_count")
    average_count_baseline = averages[baseline_index]["review_count"] if baseline_index is not None else None
    average_count_current = averages[current_index]["review_count"] if current_index is not None else None
    valid_ranks = [(axis[index], rank) for index, rank in enumerate(ranks) if rank is not None]
    baseline_rank, current_rank = (valid_ranks[0][1], valid_ranks[-1][1]) if len(valid_ranks) >= 2 else (None, None)
    return {
        **customer,
        "baseline_rank": baseline_rank, "current_rank": current_rank,
        "rank_movement": baseline_rank - current_rank if baseline_rank is not None and current_rank is not None else None,
        "baseline_rating_gap": _number(rating_baseline), "current_rating_gap": _number(rating_current),
        "rating_gap_change": _number(rating_current - rating_baseline) if rating_baseline is not None and rating_current is not None else None,
        "rating_gap_interpretation": rating_gap_interpretation(rating_baseline, rating_current),
        "baseline_review_gap": _number(count_baseline), "current_review_gap": _number(count_current),
        "review_gap_change": _number(count_current - count_baseline) if count_baseline is not None and count_current is not None else None,
        "selected_average_review_growth": _number(average_count_current - average_count_baseline) if average_count_baseline is not None and average_count_current is not None else None,
    }


def _leaders(subjects, customer):
    competitors = [item for item in subjects if not item["is_customer"]]
    rated = [item for item in competitors if item["current_rating"] is not None]
    counted = [item for item in competitors if item["current_review_count"] is not None]
    growing = [item for item in competitors if item["review_growth"] is not None and item["review_growth"] >= 0]
    improving = [item for item in competitors if item["rating_change"] is not None]
    closest = [item for item in rated if customer.get("current_rating") is not None]
    return {
        "rating_leader": max(rated, key=lambda item: (item["current_rating"], item["current_review_count"] or -1), default=None),
        "review_volume_leader": max(counted, key=lambda item: item["current_review_count"], default=None),
        "fastest_review_growth": max(growing, key=lambda item: item["review_growth"], default=None),
        "largest_rating_improvement": max(improving, key=lambda item: item["rating_change"], default=None),
        "closest_rating": min(closest, key=lambda item: abs(item["current_rating"] - customer["current_rating"]), default=None),
    }


def _data_quality(values, competitor_keys, axis, refresh_by_date, contributors, now):
    customer_dates = set(values.get("customer", {})); expected = set(axis)
    coverage = {key: round(sum(day in values.get(key, {}) for day in axis) / len(axis) * 100, 1) if axis else 0 for key in competitor_keys}
    count_decreases, rating_changes = [], []
    for key in ["customer", *competitor_keys]:
        points = [(day, values[key][day]) for day in axis if day in values.get(key, {})]
        for previous, current in zip(points, points[1:]):
            if previous[1]["review_count"] is not None and current[1]["review_count"] is not None and current[1]["review_count"] < previous[1]["review_count"]:
                count_decreases.append({"subject_key": key, "date": current[0].isoformat()})
            if previous[1]["rating"] is not None and current[1]["rating"] is not None and abs(current[1]["rating"] - previous[1]["rating"]) >= Decimal("0.5"):
                rating_changes.append({"subject_key": key, "date": current[0].isoformat()})
    partial = [day.isoformat() for day in axis if day in refresh_by_date and len(refresh_by_date[day]) < 1 + len(competitor_keys)]
    latest = axis[-1] if axis else None; today = (now or datetime.now(timezone.utc)).date()
    return {
        "expected_dates": len(axis), "available_customer_dates": len(customer_dates & expected),
        "customer_coverage_percentage": round(len(customer_dates & expected) / len(axis) * 100, 1) if axis else 0,
        "competitor_coverage_by_subject": coverage,
        "selected_average_contributor_count_by_date": contributors,
        "missing_customer_dates": [day.isoformat() for day in axis if day not in customer_dates],
        "partial_refresh_dates": partial, "anomalous_rating_changes": rating_changes,
        "anomalous_review_count_decreases": count_decreases,
        "stale_latest_snapshot": bool(latest and (today - latest).days > max(2, int(Config.COMPETITOR_REFRESH_HOURS / 24) + 1)),
        "exact_claims_allowed": bool(axis and len(axis) >= 2 and not rating_changes),
    }


def _insights(summary, leaders, quality, stage):
    if stage < 2 or not quality["exact_claims_allowed"]: return []
    span = summary.get("history_span") or 0; insights = []
    gap_change = summary.get("rating_gap_change")
    if gap_change is not None and abs(gap_change) >= 0.05:
        insights.append({"id": "rating-gap", "title": "Your rating gap narrowed" if gap_change > 0 else "The rating gap widened", "explanation": summary["rating_gap_interpretation"], "type": "positive" if gap_change > 0 else "watch", "span_days": span, "confidence": "High" if quality["customer_coverage_percentage"] >= 80 else "Moderate"})
    fastest = leaders.get("fastest_review_growth")
    own_growth = summary.get("review_growth")
    if not quality["anomalous_review_count_decreases"] and fastest and own_growth is not None and fastest.get("review_growth") is not None and fastest["review_growth"] > own_growth:
        insights.append({"id": "review-growth", "title": "A selected competitor added reviews faster", "explanation": f"You added {own_growth} reviews while {fastest['name']} added {fastest['review_growth']} during the available {span}-day period.", "type": "watch", "span_days": span, "confidence": "High" if min(summary.get("coverage_percentage", 0), fastest.get("coverage_percentage", 0)) >= 80 else "Moderate"})
    movement = summary.get("rank_movement")
    if movement:
        insights.append({"id": "rank-movement", "title": "Selected-competitor rank improved" if movement > 0 else "Selected-competitor rank declined", "explanation": f"Your rank moved from {summary['baseline_rank']} to {summary['current_rank']} among the current selected watchlist.", "type": "positive" if movement > 0 else "watch", "span_days": span, "confidence": "Moderate"})
    return insights[:4]


def _movement_label(change):
    if change is None: return "Insufficient history"
    if abs(change) < RATING_STABLE_THRESHOLD: return "Stable"
    return "Improving" if change > 0 else "Declining"


def _empty_subject(key, name):
    return _subject_metrics(key, name, {}, [])


def _as_date(value):
    if isinstance(value, datetime): return value.date()
    if isinstance(value, date): return value
    if isinstance(value, str):
        try: return date.fromisoformat(value[:10])
        except ValueError: return None
    return None


def _decimal(value):
    if value is None: return None
    try: return Decimal(str(value))
    except Exception: return None


def _integer(value):
    if value is None: return None
    try: return int(value)
    except (TypeError, ValueError): return None


def _number(value):
    return round(float(value), 2) if value is not None else None
