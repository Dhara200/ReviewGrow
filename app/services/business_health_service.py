def calculate_business_health(metrics):
    total_reviews = int(metrics.get("total_reviews") or 0)
    if total_reviews < 5:
        return {
            "score": None,
            "status": "Pending",
            "label": "Pending - Need at least 5 live Google reviews",
            "drivers": [],
        }

    average_rating = float(metrics.get("average_rating") or 0)
    negative_percent = float(metrics.get("negative_review_percentage") or 0)
    response_rate = float(metrics.get("response_rate") or 0)
    rating_change = float(metrics.get("rating_change_30_days") or metrics.get("rating_change") or 0)
    unanswered_negative = int(metrics.get("unanswered_negative_review_count") or 0)

    score = average_rating * 2
    score -= min(2.2, negative_percent / 18)
    score += min(1.0, response_rate / 100)

    if rating_change < 0:
        score -= min(1.2, abs(rating_change) * 1.6)
    elif rating_change > 0:
        score += min(0.5, rating_change)

    score -= min(1.5, unanswered_negative * 0.25)
    score = round(max(0, min(score, 10)), 1)

    status = health_status(score)
    return {
        "score": score,
        "status": status,
        "label": f"{score}/10 - {status}",
        "drivers": [
            f"Average rating: {average_rating:.1f}",
            f"Negative review share: {negative_percent:.0f}%",
            f"Response rate: {response_rate:.0f}%",
            f"Recent rating change: {rating_change:+.1f}",
            f"Unanswered negative reviews: {unanswered_negative}",
        ],
    }


def health_status(score):
    if score is None:
        return "Pending"
    if score >= 8:
        return "Excellent"
    if score >= 6.5:
        return "Good"
    if score >= 4:
        return "Needs Attention"
    return "Critical"
