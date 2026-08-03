from datetime import datetime, timedelta, timezone

from app.services.database_service import get_connection


ALLOWED_PERIODS = {7: "Last 7 days", 30: "Last 30 days", 90: "Last 90 days", 365: "Last 12 months"}
TOPIC_ALIASES = {
    "wi-fi": "wifi", "wi fi": "wifi", "internet": "wifi",
    "staff behaviour": "staff", "staff behavior": "staff",
    "customer service": "service", "waiting": "waiting time",
    "value": "value for money",
}


def resolve_period(value, now=None):
    try:
        days = int(value)
    except (TypeError, ValueError):
        days = 30
    if days not in ALLOWED_PERIODS:
        days = 30
    now_utc = _as_utc(now or datetime.now(timezone.utc))
    current_end = now_utc
    current_start = current_end - timedelta(days=days)
    previous_start = current_start - timedelta(days=days)
    return {
        "days": days,
        "label": ALLOWED_PERIODS[days],
        "current_start": current_start.replace(tzinfo=None),
        "current_end": current_end.replace(tzinfo=None),
        "previous_start": previous_start.replace(tzinfo=None),
        "previous_end": current_start.replace(tzinfo=None),
        "allowed": [{"days": key, "label": label} for key, label in ALLOWED_PERIODS.items()],
    }


def calculate_health_score(metrics):
    """Deterministic score: rating 45%, sentiment 25%, responses 20%, attention 10%.

    Review volume affects confidence only, never the score. Missing components are
    omitted and the remaining weights are rebalanced, so missing data is not treated
    as a zero-quality result.
    """
    if not metrics.get("total_reviews"):
        return {"score": None, "status": "Pending", "confidence": "insufficient", "contributors": []}
    parts = []
    if metrics.get("average_rating") is not None:
        parts.append(("rating", _clamp(float(metrics["average_rating"]) / 5 * 100), 45))
    if metrics.get("analysed_reviews"):
        sentiment = float(metrics.get("positive_percentage") or 0) + .5 * float(metrics.get("neutral_percentage") or 0)
        parts.append(("sentiment", _clamp(sentiment), 25))
        attention = 100 - (float(metrics.get("unanswered_negative_reviews") or 0) / metrics["analysed_reviews"] * 100)
        parts.append(("negative_attention", _clamp(attention), 10))
    if metrics.get("response_comparison_available"):
        parts.append(("response_rate", _clamp(float(metrics.get("response_rate") or 0)), 20))
    weight = sum(item[2] for item in parts)
    score = round(sum(value * item_weight for _, value, item_weight in parts) / weight, 1) if weight else None
    status = "Pending" if score is None else "Excellent" if score >= 85 else "Strong" if score >= 70 else "Needs Attention" if score >= 50 else "Critical"
    total = int(metrics.get("total_reviews") or 0)
    coverage = float(metrics.get("analysis_coverage") or 0)
    confidence = "high" if total >= 20 and coverage >= 80 else "medium" if total >= 5 and coverage >= 50 else "low"
    return {
        "score": score, "status": status, "confidence": confidence,
        "contributors": [{"name": name, "value": round(value, 1), "weight": item_weight} for name, value, item_weight in parts],
        "drivers": [f"{name.replace('_', ' ').title()}: {value:.0f}/100" for name, value, _ in parts[:3]],
    }


def build_consultant_data(business_id, period_value=30, google_location_id=None, now=None):
    period = resolve_period(period_value, now=now)
    connection = get_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        scope_sql, scope_params = _scope(business_id, google_location_id)
        current = _aggregate_period(cursor, scope_sql, scope_params, period["current_start"], period["current_end"])
        previous = _aggregate_period(cursor, scope_sql, scope_params, period["previous_start"], period["previous_end"])
        cursor.execute(f"SELECT COUNT(*) total_reviews, AVG(COALESCE(review_rating,rating)) average_rating FROM reviews r WHERE {scope_sql}", tuple(scope_params))
        lifetime = cursor.fetchone() or {}
        current["total_reviews"] = int(lifetime.get("total_reviews") or 0)
        current["overall_rating"] = _round_or_none(lifetime.get("average_rating"))
        kpis = _build_kpis(current, previous)
        sentiment = _build_sentiment(current, previous)
        topics = _load_topics(cursor, scope_sql, scope_params, period)
        health_metrics = {
            **current,
            "average_rating": current["overall_rating"],
            "positive_percentage": sentiment["positive_percentage"],
            "neutral_percentage": sentiment["neutral_percentage"],
            "unanswered_negative_reviews": current["unanswered_negative_reviews"],
            "analysis_coverage": sentiment["analysis_coverage_percentage"],
            "response_comparison_available": current["reviews"] > 0,
        }
        health = calculate_health_score(health_metrics)
        trend = _load_trend(cursor, scope_sql, scope_params, period)
        attention_reviews = _load_attention_reviews(cursor, scope_sql, scope_params, period, topics)
        actions = prioritize_actions(kpis, sentiment, topics, attention_reviews)
        wins = build_wins(kpis, sentiment, topics, period)
        briefing = build_executive_briefing(health, kpis, sentiment, topics, actions)
        categories = [_topic_category(topic) for topic in topics if topic["mention_count"] > 0][:8]
        data_quality = {
            "total_reviews": current["total_reviews"], "reviews_in_period": current["reviews"],
            "analysed_reviews": current["analysed_reviews"], "unanalysed_reviews": current["unanalysed_reviews"],
            "analysis_coverage_percentage": sentiment["analysis_coverage_percentage"],
            "previous_comparison_available": previous["reviews"] > 0,
            "response_timestamps_available": current["response_samples"] > 0,
            "historical_chart_available": any(point["review_count"] for point in trend["points"]),
            "topic_confidence_sufficient": any(topic["confidence"] != "low" for topic in topics),
        }
        return {
            "period": period, "summary": {"health": health, "briefing": briefing},
            "kpis": kpis, "sentiment": sentiment, "topics": topics,
            "health_categories": categories, "trend": trend, "actions": actions,
            "wins": wins, "attention_reviews": attention_reviews,
            "executive_briefing": briefing, "data_quality": data_quality,
            "forecast": {"available": False, "message": "More review history is required to generate a forecast."},
        }
    finally:
        cursor.close()
        connection.close()


def build_legacy_context(consultant_data, report=None, latest_review_at=None):
    """Compatibility adapter for the existing Phase 1 template and action workflow."""
    sentiment = consultant_data["sentiment"]
    kpis = consultant_data["kpis"]
    topics = consultant_data["topics"]
    briefing = consultant_data["executive_briefing"]
    health = consultant_data["summary"]["health"]
    metrics = {
        "total_reviews": kpis["total_reviews"]["current"] or 0,
        "average_rating": kpis["overall_rating"]["current"] or 0,
        "recent_review_count": kpis["new_reviews"]["current"] or 0,
        "response_rate": kpis["response_rate"]["current"] or 0,
        "positive_review_count": sentiment["positive"], "neutral_review_count": sentiment["neutral"],
        "negative_review_count": sentiment["negative"],
        "positive_review_percentage": sentiment["positive_percentage"] or 0,
        "neutral_review_percentage": sentiment["neutral_percentage"] or 0,
        "negative_review_percentage": sentiment["negative_percentage"] or 0,
        "unanswered_review_count": kpis["unanswered_reviews"]["current"] or 0,
        "unanswered_negative_review_count": sum(1 for item in consultant_data["attention_reviews"] if item["reply_status"] == "pending" and item["rating"] <= 2),
        "top_positive_topics": [{"topic": item["topic"], "count": item["positive_count"]} for item in topics if item["positive_count"]][:5],
        "top_negative_topics": [{"topic": item["topic"], "count": item["negative_count"]} for item in topics if item["negative_count"]][:5],
    }
    action_plan = [{"title": item["title"], "reason": item["evidence"], "priority": item["priority"],
                    "impact": item["expected_impact"], "owner_action": item["owner_action"], "topic": item["related_topic"]}
                   for item in consultant_data["actions"]]
    change = lambda name: kpis[name]["absolute_change"] if kpis[name]["comparison_available"] else 0
    trend_summary = {"last_30_days": {"rating_change": change("overall_rating"), "review_volume_change": change("new_reviews"),
        "positive_review_change": sentiment["positive_change"]["absolute_change"] or 0 if sentiment["positive_change"]["comparison_available"] else 0,
        "negative_review_change": sentiment["negative_change"]["absolute_change"] or 0 if sentiment["negative_change"]["comparison_available"] else 0,
        "most_increased_negative_topic": next(({"topic": item["topic"], "percentage_change": item["mention_change"]} for item in topics if item["negative_count"] and item["mention_change"] > 0), None)},
        "this_month": {"rating_change": change("overall_rating"), "review_volume_change": change("new_reviews"),
                       "positive_review_change": 0, "negative_review_change": 0}}
    report_state = "none" if not report else "outdated" if report.get("report_status") == "outdated" else "up_to_date"
    return metrics, {
        "metrics": metrics, "health": health,
        "daily_briefing": briefing["recommended_focus"],
        "executive_summary": briefing["overall_assessment"],
        "alerts": [{"title": item["title"], "message": item["evidence"], "priority": item["priority"], "type": "warning"} for item in consultant_data["actions"][:3]],
        "action_plan": action_plan,
        "strengths": [item["topic"] for item in topics if item["status"] == "Strength"][:5],
        "weaknesses": [item["topic"] for item in topics if item["status"] in {"Critical", "Watch"}][:5],
        "topic_analytics": [{"topic": item["topic"], "sentiment": item["status"], "count": item["mention_count"], "percentage": item["positive_percentage"]} for item in topics],
        "emotion_breakdown": [], "trend_summary": trend_summary,
        "latest_attention_reviews": consultant_data["attention_reviews"],
        "opportunities": [item["evidence"] for item in consultant_data["wins"]],
        "risks": [item["evidence"] for item in consultant_data["actions"] if item["priority"] == "High"],
        "latest_review_at": latest_review_at,
        "report_status": {"state": report_state, "label": report_state.replace("_", " ").title()},
        "data_source": "Live Google Reviews Only",
    }


def _aggregate_period(cursor, scope_sql, params, start, end):
    cursor.execute(f"""
        SELECT COUNT(*) reviews, AVG(COALESCE(review_rating,rating)) average_rating,
          SUM(LOWER(sentiment)='positive') positive_reviews,
          SUM(LOWER(sentiment)='neutral') neutral_reviews,
          SUM(LOWER(sentiment)='negative') negative_reviews,
          SUM(sentiment IS NOT NULL AND LOWER(sentiment) IN ('positive','neutral','negative')) analysed_reviews,
          SUM(sentiment IS NULL OR LOWER(sentiment) NOT IN ('positive','neutral','negative')) unanalysed_reviews,
          SUM(reply_status='pending') unanswered_reviews,
          SUM(reply_status='pending' AND (LOWER(sentiment)='negative' OR COALESCE(review_rating,rating)<=2)) unanswered_negative_reviews,
          SUM(reply_status IN ('approved','posted')) answered_reviews,
          AVG(CASE WHEN reply_status IN ('approved','posted') AND COALESCE(replied_at,reply_posted_at) IS NOT NULL
            AND COALESCE(replied_at,reply_posted_at)>=COALESCE(review_created_at,review_date,created_at)
            THEN TIMESTAMPDIFF(MINUTE, COALESCE(review_created_at,review_date,created_at), COALESCE(replied_at,reply_posted_at)) END) average_response_minutes,
          SUM(reply_status IN ('approved','posted') AND COALESCE(replied_at,reply_posted_at) IS NOT NULL
            AND COALESCE(replied_at,reply_posted_at)>=COALESCE(review_created_at,review_date,created_at)) response_samples
        FROM reviews r WHERE {scope_sql}
          AND COALESCE(review_created_at,review_date,created_at)>=%s
          AND COALESCE(review_created_at,review_date,created_at)<%s
    """, tuple([*params, start, end]))
    row = cursor.fetchone() or {}
    result = {key: int(row.get(key) or 0) for key in (
        "reviews", "positive_reviews", "neutral_reviews", "negative_reviews", "analysed_reviews",
        "unanalysed_reviews", "unanswered_reviews", "unanswered_negative_reviews", "answered_reviews", "response_samples")}
    result["average_rating"] = _round_or_none(row.get("average_rating"))
    result["average_response_minutes"] = _round_or_none(row.get("average_response_minutes"), 0)
    result["response_rate"] = round(result["answered_reviews"] / result["reviews"] * 100, 1) if result["reviews"] else None
    return result


def _build_kpis(current, previous):
    definitions = {
        "overall_rating": (current["overall_rating"], previous["average_rating"]),
        "total_reviews": (current["total_reviews"], None), "new_reviews": (current["reviews"], previous["reviews"]),
        "positive_reviews": (current["positive_reviews"], previous["positive_reviews"]),
        "neutral_reviews": (current["neutral_reviews"], previous["neutral_reviews"]),
        "negative_reviews": (current["negative_reviews"], previous["negative_reviews"]),
        "unanswered_reviews": (current["unanswered_reviews"], previous["unanswered_reviews"]),
        "average_response_minutes": (current["average_response_minutes"], previous["average_response_minutes"]),
        "response_rate": (current["response_rate"], previous["response_rate"]),
        "review_growth": (current["reviews"], previous["reviews"]),
    }
    return {name: _comparison(value, old) for name, (value, old) in definitions.items()}


def _comparison(current, previous):
    available = current is not None and previous is not None
    absolute = round(current - previous, 2) if available else None
    percentage = round(absolute / abs(previous) * 100, 1) if available and previous != 0 else None
    return {"current": current, "previous": previous, "absolute_change": absolute, "percentage_change": percentage,
            "direction": "up" if available and absolute > 0 else "down" if available and absolute < 0 else "stable" if available else "unavailable",
            "comparison_available": available, "zero_baseline": available and previous == 0}


def _build_sentiment(current, previous):
    analysed = current["analysed_reviews"]
    result = {name: current[f"{name}_reviews"] for name in ("positive", "neutral", "negative")}
    for name in ("positive", "neutral", "negative"):
        result[f"{name}_percentage"] = round(result[name] / analysed * 100, 1) if analysed else None
        old = round(previous[f"{name}_reviews"] / previous["analysed_reviews"] * 100, 1) if previous["analysed_reviews"] else None
        result[f"{name}_change"] = _comparison(result[f"{name}_percentage"], old)
    result.update({"analysed_reviews": analysed, "unanalysed_reviews": current["unanalysed_reviews"],
                   "analysis_coverage_percentage": round(analysed / current["reviews"] * 100, 1) if current["reviews"] else None})
    return result


def normalize_topic(value):
    normalized = " ".join(str(value or "").strip().lower().replace("_", " ").split())
    return TOPIC_ALIASES.get(normalized, normalized)


def _load_topics(cursor, scope_sql, params, period):
    cursor.execute(f"""
      SELECT rt.topic, rt.sentiment,
        SUM(COALESCE(r.review_created_at,r.review_date,r.created_at)>=%s AND COALESCE(r.review_created_at,r.review_date,r.created_at)<%s) current_count,
        SUM(COALESCE(r.review_created_at,r.review_date,r.created_at)>=%s AND COALESCE(r.review_created_at,r.review_date,r.created_at)<%s) previous_count
      FROM review_topics rt JOIN reviews r ON r.id=rt.review_id WHERE {scope_sql}
      GROUP BY rt.topic,rt.sentiment
    """, tuple([period["current_start"], period["current_end"], period["previous_start"], period["previous_end"], *params]))
    grouped = {}
    for row in cursor.fetchall():
        name = normalize_topic(row["topic"])
        if not name: continue
        item = grouped.setdefault(name, {"topic": name, "positive_count": 0, "neutral_count": 0, "negative_count": 0, "previous_mentions": 0})
        sentiment = str(row.get("sentiment") or "").lower()
        if sentiment in {"positive", "neutral", "negative"}: item[f"{sentiment}_count"] += int(row.get("current_count") or 0)
        item["previous_mentions"] += int(row.get("previous_count") or 0)
    topics = []
    for item in grouped.values():
        mentions = item["positive_count"] + item["neutral_count"] + item["negative_count"]
        if not mentions: continue
        positive = round(item["positive_count"] / mentions * 100, 1)
        negative = round(item["negative_count"] / mentions * 100, 1)
        mention_change = mentions - item["previous_mentions"]
        confidence = "high" if mentions >= 10 else "medium" if mentions >= 3 else "low"
        status = "Critical" if mentions >= 3 and negative >= 60 else "Watch" if negative >= 35 or mention_change >= 3 and item["negative_count"] else "Strength" if mentions >= 3 and positive >= 70 else "Stable"
        topics.append({**item, "mention_count": mentions, "positive_percentage": positive, "negative_percentage": negative,
                       "mention_change": mention_change, "mention_trend": "up" if mention_change > 0 else "down" if mention_change < 0 else "stable",
                       "sentiment_trend": "unavailable", "status": status, "confidence": confidence,
                       "filter": name})
    return sorted(topics, key=lambda item: (item["status"] not in {"Critical", "Watch"}, -item["mention_count"]))[:12]


def _topic_category(topic):
    score = round(_clamp(topic["positive_percentage"] + .5 * (100 - topic["positive_percentage"] - topic["negative_percentage"])), 1)
    return {"name": topic["topic"], "mention_count": topic["mention_count"], "positive_percentage": topic["positive_percentage"],
            "negative_percentage": topic["negative_percentage"], "health_score": score, "trend": topic["mention_trend"],
            "status": topic["status"], "confidence": topic["confidence"]}


def _load_trend(cursor, scope_sql, params, period):
    cursor.execute(f"""SELECT DATE(COALESCE(review_created_at,review_date,created_at)) review_day,
      COUNT(*) review_count, AVG(COALESCE(review_rating,rating)) rating,
      SUM(LOWER(sentiment)='positive') positive, SUM(LOWER(sentiment)='neutral') neutral, SUM(LOWER(sentiment)='negative') negative,
      SUM(reply_status IN ('approved','posted')) answered
      FROM reviews r WHERE {scope_sql} AND COALESCE(review_created_at,review_date,created_at)>=%s AND COALESCE(review_created_at,review_date,created_at)<%s
      GROUP BY DATE(COALESCE(review_created_at,review_date,created_at)) ORDER BY review_day""", tuple([*params, period["current_start"], period["current_end"]]))
    rows = cursor.fetchall()
    return {"granularity": "daily" if period["days"] <= 30 else "weekly" if period["days"] <= 90 else "monthly",
            "points": build_trend_buckets(rows, period)}


def build_trend_buckets(rows, period):
    granularity = "daily" if period["days"] <= 30 else "weekly" if period["days"] <= 90 else "monthly"
    buckets = {}
    current = period["current_start"].date()
    end = period["current_end"].date()
    while current <= end:
        key = _bucket_key(current, granularity)
        buckets.setdefault(key, {"date": key.isoformat(), "review_count": 0, "rating_sum": 0.0, "rating_count": 0, "positive": 0, "neutral": 0, "negative": 0, "answered": 0})
        current += timedelta(days=1)
    for row in rows:
        day = row["review_day"] if hasattr(row["review_day"], "year") else datetime.fromisoformat(str(row["review_day"])).date()
        bucket = buckets.setdefault(_bucket_key(day, granularity), {"date": _bucket_key(day, granularity).isoformat(), "review_count": 0, "rating_sum": 0.0, "rating_count": 0, "positive": 0, "neutral": 0, "negative": 0, "answered": 0})
        count = int(row.get("review_count") or 0); rating = row.get("rating")
        bucket["review_count"] += count
        if rating is not None: bucket["rating_sum"] += float(rating) * count; bucket["rating_count"] += count
        for key in ("positive", "neutral", "negative", "answered"): bucket[key] += int(row.get(key) or 0)
    points = []
    for bucket in sorted(buckets.values(), key=lambda value: value["date"]):
        analysed = bucket["positive"] + bucket["neutral"] + bucket["negative"]
        points.append({"label": bucket["date"], "review_count": bucket["review_count"],
                       "rating": round(bucket["rating_sum"] / bucket["rating_count"], 2) if bucket["rating_count"] else None,
                       "positive_percentage": round(bucket["positive"] / analysed * 100, 1) if analysed else None,
                       "response_rate": round(bucket["answered"] / bucket["review_count"] * 100, 1) if bucket["review_count"] else None})
    return points


def _load_attention_reviews(cursor, scope_sql, params, period, topics):
    severe_topics = {item["topic"] for item in topics if item["status"] in {"Critical", "Watch"}}
    cursor.execute(f"""SELECT r.id,COALESCE(r.review_rating,r.rating) rating,r.review_text,r.sentiment,r.reply_status,
      COALESCE(r.review_created_at,r.review_date,r.created_at) review_date,
      (SELECT rt.topic FROM review_topics rt WHERE rt.review_id=r.id ORDER BY (rt.sentiment='negative') DESC,rt.confidence DESC LIMIT 1) topic
      FROM reviews r WHERE {scope_sql} AND COALESCE(review_created_at,review_date,created_at)>=%s AND COALESCE(review_created_at,review_date,created_at)<%s
      AND (COALESCE(review_rating,rating)<=2 OR LOWER(sentiment)='negative' OR reply_status='pending')
      ORDER BY (reply_status='pending' AND COALESCE(review_rating,rating)<=2) DESC, COALESCE(review_rating,rating) ASC,
      COALESCE(review_created_at,review_date,created_at) DESC LIMIT 12""", tuple([*params, period["current_start"], period["current_end"]]))
    ranked = []
    for row in cursor.fetchall():
        topic = normalize_topic(row.get("topic")) or "general experience"
        rating = float(row.get("rating") or 0)
        score = (50 if row.get("reply_status") == "pending" else 0) + (30 if rating <= 2 else 0) + (20 if str(row.get("sentiment") or "").lower() == "negative" else 0) + (10 if topic in severe_topics else 0)
        ranked.append((score, {"id": int(row["id"]), "rating": rating,
            "review_text": (row.get("review_text") or "")[:240], "sentiment": row.get("sentiment") or "unanalysed", "reply_status": row.get("reply_status") or "pending",
            "review_date": str(row.get("review_date") or ""), "topic": topic,
            "suggested_action": "Respond promptly and address the specific concern." if row.get("reply_status") == "pending" else "Review the recurring issue and confirm the operational fix.",
            "url": f"/reviews/history/{params[0]}?open_review={row['id']}"}))
    return [item for _, item in sorted(ranked, key=lambda pair: pair[0], reverse=True)[:5]]


def prioritize_actions(kpis, sentiment, topics, attention_reviews):
    actions = []
    unanswered = kpis["unanswered_reviews"]["current"] or 0
    severe = next((topic for topic in topics if topic["status"] in {"Critical", "Watch"}), None)
    if unanswered:
        actions.append(_action("unanswered-reviews", "High", f"Respond to {unanswered} unanswered review{'s' if unanswered != 1 else ''}",
            f"{unanswered} review{'s remain' if unanswered != 1 else ' remains'} without an approved or posted owner response.",
            "Open the review queue, prioritise low ratings, and publish a specific response.", "High potential impact", "review responses", unanswered, "high"))
    if severe:
        actions.append(_action(f"topic:{severe['topic']}", "High" if severe["status"] == "Critical" else "Medium", f"Investigate recurring {severe['topic']} feedback",
            f"{severe['topic'].title()} appears in {severe['mention_count']} analysed topic mentions; {severe['negative_percentage']}% are negative.",
            "Review the related feedback, assign an operational owner, and check the issue weekly.", "May reduce repeated complaints", severe["topic"], severe["mention_count"], severe["confidence"]))
    negative_change = sentiment["negative_change"]
    if negative_change["comparison_available"] and (negative_change["absolute_change"] or 0) > 5:
        actions.append(_action("negative-sentiment-rise", "High", "Review the rise in negative sentiment",
            f"Negative sentiment increased by {negative_change['absolute_change']:.1f} percentage points versus the previous period.",
            "Compare recent complaints with the previous period and identify the repeated operational change.", "Recommended immediate attention", "sentiment", sentiment["negative"], "medium"))
    positive_topic = next((topic for topic in topics if topic["status"] == "Strength"), None)
    if positive_topic:
        actions.append(_action(f"reinforce:{positive_topic['topic']}", "Quick win", f"Reinforce your strength in {positive_topic['topic']}",
            f"{positive_topic['positive_percentage']}% of {positive_topic['mention_count']} mentions are positive.",
            "Recognise the team behavior behind this strength and reference it naturally in review responses.", "Likely to reinforce customer satisfaction", positive_topic["topic"], positive_topic["mention_count"], positive_topic["confidence"]))
    if not actions and attention_reviews:
        actions.append(_action("review-attention", "Medium", "Review recent customer concerns", f"{len(attention_reviews)} recent review(s) require attention.", "Read each review and record the operational follow-up.", "May improve response quality", "general experience", len(attention_reviews), "medium"))
    return actions[:5]


def _action(identifier, priority, title, evidence, owner_action, impact, topic, count, confidence):
    return {"id": identifier, "priority": priority, "title": title, "explanation": evidence, "evidence": evidence,
            "owner_action": owner_action, "expected_impact": impact, "related_topic": topic, "related_review_count": count,
            "related_review_ids": [], "confidence": confidence}


def build_wins(kpis, sentiment, topics, period):
    wins = []
    rating = kpis["overall_rating"]
    if rating["comparison_available"] and (rating["absolute_change"] or 0) > 0:
        wins.append({"label": "Rating improved", "value": f"+{rating['absolute_change']:.2f}", "evidence": "Average rating increased versus the previous equivalent period.", "comparison_period": period["label"], "confidence": "high"})
    positive = sentiment["positive_change"]
    if positive["comparison_available"] and (positive["absolute_change"] or 0) > 0:
        wins.append({"label": "Positive sentiment grew", "value": f"+{positive['absolute_change']:.1f} pts", "evidence": "The positive share of analysed reviews increased.", "comparison_period": period["label"], "confidence": "medium"})
    strength = next((topic for topic in topics if topic["status"] == "Strength"), None)
    if strength:
        wins.append({"label": f"{strength['topic'].title()} is a strength", "value": f"{strength['positive_percentage']}% positive", "evidence": f"Based on {strength['mention_count']} topic mentions.", "comparison_period": period["label"], "confidence": strength["confidence"]})
    return wins[:4]


def build_executive_briefing(health, kpis, sentiment, topics, actions):
    rating = kpis["overall_rating"]["current"]
    strongest = max(topics, key=lambda item: item["positive_count"], default=None)
    complaint = max(topics, key=lambda item: item["negative_count"], default=None)
    improving = "No reliable positive movement is available for comparison."
    if kpis["overall_rating"]["comparison_available"] and (kpis["overall_rating"]["absolute_change"] or 0) > 0:
        improving = f"Average rating improved by {kpis['overall_rating']['absolute_change']:.2f} points."
    declined = "No significant decline was detected from the available comparison."
    if sentiment["negative_change"]["comparison_available"] and (sentiment["negative_change"]["absolute_change"] or 0) > 0:
        declined = f"Negative sentiment increased by {sentiment['negative_change']['absolute_change']:.1f} percentage points."
    return {
        "overall_assessment": f"Reputation health is {health['status'].lower()} at {health['score']:.0f}/100 with an overall rating of {rating:.1f}." if health["score"] is not None and rating is not None else "More review data is required for a reliable assessment.",
        "what_improved": improving, "what_declined": declined,
        "strongest_praise": f"{strongest['topic'].title()} is the strongest praise signal with {strongest['positive_count']} positive mention(s)." if strongest and strongest["positive_count"] else "No reliable praise topic is available yet.",
        "most_frequent_complaint": f"{complaint['topic'].title()} is the most frequent complaint with {complaint['negative_count']} negative mention(s)." if complaint and complaint["negative_count"] else "No repeated complaint topic is established yet.",
        "emerging_issue": next((f"{topic['topic'].title()} mentions increased by {topic['mention_change']} versus the previous period." for topic in topics if topic["negative_count"] and topic["mention_change"] > 0), "No emerging issue is supported by the current sample."),
        "recommended_focus": actions[0]["owner_action"] if actions else "Continue monitoring new reviews and maintain timely responses.",
    }


def _scope(business_id, google_location_id):
    sql = "r.business_id=%s AND LOWER(COALESCE(r.source_platform,r.source,''))='google' AND r.google_review_id IS NOT NULL"
    params = [business_id]
    if google_location_id:
        sql += " AND r.google_location_id=%s"; params.append(google_location_id)
    return sql, params


def _bucket_key(value, granularity):
    if granularity == "weekly": return value - timedelta(days=value.weekday())
    if granularity == "monthly": return value.replace(day=1)
    return value


def _as_utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _round_or_none(value, digits=2):
    return None if value is None else round(float(value), digits)


def _clamp(value):
    return max(0, min(float(value), 100))
