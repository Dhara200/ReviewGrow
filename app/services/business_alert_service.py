def build_business_alerts(metrics, trend_summary, topic_analytics):
    alerts = []
    attention_count = int(metrics.get("critical_negative_review_count") or 0)
    unanswered_negative = int(metrics.get("unanswered_negative_review_count") or 0)
    response_rate = float(metrics.get("response_rate") or 0)
    rating_change = float(trend_summary.get("last_30_days", {}).get("rating_change") or 0)
    negative_change = int(trend_summary.get("last_30_days", {}).get("negative_review_change") or 0)
    increased_topic = trend_summary.get("last_30_days", {}).get("most_increased_negative_topic")

    if attention_count:
        alerts.append({
            "title": f"{attention_count} critical negative review{'s' if attention_count != 1 else ''}",
            "message": "Recent 1-star or 2-star reviews need same-day service recovery.",
            "priority": "High",
            "type": "danger",
        })

    if unanswered_negative:
        alerts.append({
            "title": f"{unanswered_negative} unanswered negative review{'s' if unanswered_negative != 1 else ''}",
            "message": "Unanswered negative reviews can reduce trust for customers comparing businesses.",
            "priority": "High",
            "type": "danger",
        })

    if rating_change <= -0.2:
        alerts.append({
            "title": "Rating dropped recently",
            "message": f"Average rating is down {abs(rating_change):.1f} points versus the previous 30 days.",
            "priority": "High",
            "type": "warning",
        })

    if increased_topic:
        topic_text = increased_topic.get("topic")
        percent = increased_topic.get("percentage_change")
        alerts.append({
            "title": f"{topic_text.title()} complaints increased",
            "message": f"{topic_text.title()} negative mentions rose by {percent}% in the last 30 days.",
            "priority": "Medium",
            "type": "warning",
        })
    elif negative_change > 0:
        alerts.append({
            "title": "Negative review volume increased",
            "message": f"Negative reviews increased by {negative_change} compared with the previous 30 days.",
            "priority": "Medium",
            "type": "warning",
        })

    if response_rate < 60 and int(metrics.get("total_reviews") or 0) >= 5:
        alerts.append({
            "title": "Low response rate",
            "message": f"Only {response_rate:.0f}% of reviews are marked answered. Clear negative replies first.",
            "priority": "Medium",
            "type": "warning",
        })

    positive_change = int(trend_summary.get("last_30_days", {}).get("positive_review_change") or 0)
    if positive_change > 0:
        alerts.append({
            "title": "Positive trend highlight",
            "message": f"Positive reviews increased by {positive_change} in the last 30 days.",
            "priority": "Low",
            "type": "success",
        })

    if not alerts:
        top_positive = next(
            (row for row in topic_analytics if row.get("sentiment") == "positive"),
            None,
        )
        alerts.append({
            "title": "No urgent alerts",
            "message": (
                f"Keep reinforcing {top_positive['topic']} while monitoring new low-rating reviews."
                if top_positive else
                "No urgent review pattern detected. Keep syncing and responding consistently."
            ),
            "priority": "Low",
            "type": "success",
        })

    return alerts[:6]
