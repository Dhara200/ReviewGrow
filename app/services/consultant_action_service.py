from app.services.database_service import get_connection


ACTIVE_STATUSES = {"open", "in_progress"}
COMPLETED_STATUSES = {"completed"}
SUCCESS_STATUSES = {"verified"}
VALID_STATUSES = ACTIVE_STATUSES | COMPLETED_STATUSES | SUCCESS_STATUSES | {"ignored"}


def sync_consultant_actions(business_id, snapshot, report_id=None):
    """Turns AI recommendations into tracked tasks and verifies past work."""
    recommendations = _recommendations_from_snapshot(snapshot)
    recent_issues = _recent_negative_topic_issues(business_id)
    issue_by_topic = {issue["topic"]: issue for issue in recent_issues}

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        for item in recommendations:
            topic = _normalize_topic(item.get("topic") or item.get("title"))
            if not topic:
                topic = "general experience"
            issue = issue_by_topic.get(topic)
            first_detected = issue.get("first_detected_at") if issue else None
            last_detected = issue.get("last_detected_at") if issue else None
            _upsert_action(cursor, business_id, report_id, topic, item, first_detected, last_detected)

        _verify_completed_actions(cursor, business_id)
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    return get_consultant_actions(business_id)


def get_consultant_actions(business_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT *
            FROM consultant_actions
            WHERE business_id=%s
            ORDER BY
                CASE status
                    WHEN 'open' THEN 0
                    WHEN 'in_progress' THEN 1
                    WHEN 'completed' THEN 2
                    WHEN 'verified' THEN 3
                    WHEN 'ignored' THEN 4
                    ELSE 5
                END,
                COALESCE(last_detected_review_date, last_detected_at, updated_at) DESC
            """,
            (business_id,)
        )
        rows = cursor.fetchall()
        events = _events_for_actions(cursor, business_id, [row["id"] for row in rows])
        for row in rows:
            row["events"] = events.get(row["id"], [])
            row["is_reopened"] = any(
                event["event_type"] == "reopened"
                for event in row["events"]
            )
            row["verification_badge"] = _verification_badge(row)

        analytics = _action_analytics(rows)
        return {
            "active": [row for row in rows if row["status"] in ACTIVE_STATUSES],
            "completed": [row for row in rows if row["status"] == "completed"],
            "verified": [row for row in rows if row["status"] == "verified"],
            "ignored": [row for row in rows if row["status"] == "ignored"],
            "success_stories": _success_stories(cursor, business_id, rows),
            "analytics": analytics,
            "all": rows,
        }
    finally:
        cursor.close()
        conn.close()


def update_consultant_action_status(action_id, business_id, status, owner_note=None):
    if status not in VALID_STATUSES:
        raise ValueError("Invalid action status.")

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT status
            FROM consultant_actions
            WHERE id=%s
            AND business_id=%s
            """,
            (action_id, business_id)
        )
        existing = cursor.fetchone()
        if not existing:
            return False

        timestamp_sql = {
            "in_progress": "started_at=COALESCE(started_at, NOW()),",
            "completed": "completed_at=NOW(),",
            "ignored": "ignored_at=NOW(),",
        }.get(status, "")
        note_sql = "owner_note=%s," if owner_note is not None else ""
        params = []
        if owner_note is not None:
            params.append(owner_note)
        params.extend([status, action_id, business_id])

        cursor.execute(
            f"""
            UPDATE consultant_actions
            SET {note_sql}
                {timestamp_sql}
                status=%s
            WHERE id=%s
            AND business_id=%s
            """,
            tuple(params)
        )
        event_type = (
            "note_updated"
            if owner_note is not None and status == existing.get("status")
            else _event_type_for_status(status)
        )
        _log_event(
            cursor,
            action_id,
            business_id,
            event_type,
            owner_note,
        )
        conn.commit()
        return True
    finally:
        cursor.close()
        conn.close()


def filter_active_alerts(alerts, action_state):
    suppressed_topics = {
        row["topic"]
        for row in action_state.get("completed", [])
        + action_state.get("verified", [])
        + action_state.get("ignored", [])
    }
    if not suppressed_topics:
        return alerts

    filtered = []
    for alert in alerts:
        text = f"{alert.get('title', '')} {alert.get('message', '')}".lower()
        if any(topic in text for topic in suppressed_topics):
            continue
        filtered.append(alert)
    return filtered


def _upsert_action(cursor, business_id, report_id, topic, item, first_detected, last_detected):
    issue_title = item.get("title") or _issue_title(topic)
    recommendation = item.get("owner_action") or item.get("recommendation") or ""
    reason = item.get("reason") or ""
    priority = item.get("priority") or "Medium"
    impact = item.get("impact") or item.get("estimated_impact") or "Medium"

    cursor.execute(
        """
        SELECT *
        FROM consultant_actions
        WHERE business_id=%s
        AND topic=%s
        AND issue_title=%s
        LIMIT 1
        """,
        (business_id, topic, issue_title)
    )
    existing = cursor.fetchone()

    if not existing:
        cursor.execute(
            """
            INSERT INTO consultant_actions
            (
                business_id,
                report_id,
                topic,
                issue_title,
                recommendation,
                reason,
                priority,
                estimated_impact,
                status,
                first_detected_at,
                last_detected_at,
                last_detected_review_date
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'open',%s,%s,%s)
            """,
            (
                business_id,
                report_id,
                topic,
                issue_title,
                recommendation,
                reason,
                priority,
                impact,
                first_detected,
                last_detected,
                last_detected,
            )
        )
        _log_event(cursor, cursor.lastrowid, business_id, "detected", "Issue detected")
        return

    if existing["status"] == "ignored":
        return

    if existing["status"] in SUCCESS_STATUSES and not _has_negative_after(
        cursor,
        business_id,
        topic,
        existing.get("completed_at") or existing.get("updated_at"),
    ):
        return

    if existing["status"] in COMPLETED_STATUSES | SUCCESS_STATUSES and _has_negative_after(
        cursor,
        business_id,
        topic,
        existing.get("completed_at") or existing.get("updated_at"),
    ):
        cursor.execute(
            """
            UPDATE consultant_actions
            SET status='open',
                recommendation=%s,
                reason=%s,
                priority=%s,
                estimated_impact=%s,
                last_detected_at=%s,
                last_detected_review_date=%s
            WHERE id=%s
            """,
            (
                f"Issue Returned: {recommendation}",
                reason,
                priority,
                impact,
                last_detected,
                last_detected,
                existing["id"],
            )
        )
        _log_event(
            cursor,
            existing["id"],
            business_id,
            "reopened",
            "Issue reappeared after completion.",
        )
        return

    if existing["status"] in ACTIVE_STATUSES:
        cursor.execute(
            """
            UPDATE consultant_actions
            SET report_id=COALESCE(%s, report_id),
                recommendation=%s,
                reason=%s,
                priority=%s,
                estimated_impact=%s,
                last_detected_at=COALESCE(%s, last_detected_at),
                last_detected_review_date=COALESCE(%s, last_detected_review_date)
            WHERE id=%s
            """,
            (
                report_id,
                recommendation,
                reason,
                priority,
                impact,
                last_detected,
                last_detected,
                existing["id"],
            )
        )


def _verify_completed_actions(cursor, business_id):
    cursor.execute(
        """
        SELECT *
        FROM consultant_actions
        WHERE business_id=%s
        AND status='completed'
        AND completed_at IS NOT NULL
        """,
        (business_id,)
    )
    for action in cursor.fetchall():
        if _has_negative_after(cursor, business_id, action["topic"], action["completed_at"]):
            cursor.execute(
                """
                UPDATE consultant_actions
                SET status='open',
                    recommendation=CONCAT('Issue Returned: ', recommendation),
                    last_detected_review_date=(
                        SELECT MAX(COALESCE(r.review_created_at, r.review_date, r.created_at))
                        FROM review_topics rt
                        JOIN reviews r ON r.id = rt.review_id
                        WHERE rt.business_id=%s
                        AND rt.topic=%s
                        AND rt.sentiment='negative'
                        AND r.source='google'
                        AND r.google_review_id IS NOT NULL
                        AND COALESCE(r.review_created_at, r.review_date, r.created_at) > %s
                    )
                WHERE id=%s
                """,
                (business_id, action["topic"], action["completed_at"], action["id"])
            )
            _log_event(cursor, action["id"], business_id, "reopened", "Issue reappeared after completion.")
            continue

        before_count = _negative_topic_count(
            cursor,
            business_id,
            action["topic"],
            "DATE_SUB(%s, INTERVAL 30 DAY)",
            "%s",
            [action["completed_at"], action["completed_at"]],
        )
        after_count = _negative_topic_count(
            cursor,
            business_id,
            action["topic"],
            "%s",
            "NOW()",
            [action["completed_at"]],
        )
        if before_count > 0 and after_count == 0:
            cursor.execute(
                """
                UPDATE consultant_actions
                SET status='verified'
                WHERE id=%s
                """,
                (action["id"],)
            )
            _log_event(
                cursor,
                action["id"],
                business_id,
                "verified",
                "Verified improvement: negative mentions disappeared after completion.",
            )


def _recent_negative_topic_issues(business_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT
                rt.topic,
                COUNT(*) AS count,
                MIN(COALESCE(r.review_created_at, r.review_date, r.created_at)) AS first_detected_at,
                MAX(COALESCE(r.review_created_at, r.review_date, r.created_at)) AS last_detected_at
            FROM review_topics rt
            JOIN reviews r
                ON r.id = rt.review_id
            WHERE rt.business_id=%s
            AND rt.sentiment='negative'
            AND r.source='google'
            AND r.google_review_id IS NOT NULL
            AND COALESCE(r.review_created_at, r.review_date, r.created_at) >= DATE_SUB(NOW(), INTERVAL 30 DAY)
            GROUP BY rt.topic
            ORDER BY count DESC, last_detected_at DESC
            LIMIT 10
            """,
            (business_id,)
        )
        return [
            {
                "topic": _normalize_topic(row["topic"]),
                "count": int(row["count"] or 0),
                "first_detected_at": row.get("first_detected_at"),
                "last_detected_at": row.get("last_detected_at"),
            }
            for row in cursor.fetchall()
        ]
    finally:
        cursor.close()
        conn.close()


def _recommendations_from_snapshot(snapshot):
    recommendations = []
    for item in snapshot.get("action_plan", []):
        if not isinstance(item, dict):
            continue
        topic = _topic_from_action(item)
        recommendations.append({**item, "topic": topic})
    return recommendations


def _topic_from_action(item):
    text = f"{item.get('title', '')} {item.get('reason', '')} {item.get('owner_action', '')}".lower()
    known = [
        "parking", "service", "staff", "cleanliness", "waiting time", "wifi",
        "food", "room", "pricing", "delivery", "location", "customer support",
    ]
    for topic in known:
        if topic in text:
            return topic
    words = [word for word in text.replace(":", " ").split() if len(word) > 3]
    return words[0] if words else "general experience"


def _has_negative_after(cursor, business_id, topic, after_date):
    if not after_date:
        return False
    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM review_topics rt
        JOIN reviews r ON r.id = rt.review_id
        WHERE rt.business_id=%s
        AND rt.topic=%s
        AND rt.sentiment='negative'
        AND r.source='google'
        AND r.google_review_id IS NOT NULL
        AND COALESCE(r.review_created_at, r.review_date, r.created_at) > %s
        """,
        (business_id, topic, after_date)
    )
    row = cursor.fetchone() or {}
    return int(row.get("count") or 0) > 0


def _negative_topic_count(cursor, business_id, topic, start_sql, end_sql, date_params):
    cursor.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM review_topics rt
        JOIN reviews r ON r.id = rt.review_id
        WHERE rt.business_id=%s
        AND rt.topic=%s
        AND rt.sentiment='negative'
        AND r.source='google'
        AND r.google_review_id IS NOT NULL
        AND COALESCE(r.review_created_at, r.review_date, r.created_at) >= {start_sql}
        AND COALESCE(r.review_created_at, r.review_date, r.created_at) < {end_sql}
        """,
        tuple([business_id, topic, *date_params])
    )
    row = cursor.fetchone() or {}
    return int(row.get("count") or 0)


def _events_for_actions(cursor, business_id, action_ids):
    if not action_ids:
        return {}
    placeholders = ",".join(["%s"] * len(action_ids))
    cursor.execute(
        f"""
        SELECT *
        FROM consultant_action_events
        WHERE business_id=%s
        AND action_id IN ({placeholders})
        ORDER BY created_at ASC
        """,
        tuple([business_id, *action_ids])
    )
    events = {}
    for row in cursor.fetchall():
        events.setdefault(row["action_id"], []).append(row)
    return events


def _success_stories(cursor, business_id, rows):
    stories = []
    for row in rows:
        if row["status"] != "verified":
            continue
        before_count = _negative_topic_count(
            cursor,
            business_id,
            row["topic"],
            "DATE_SUB(%s, INTERVAL 30 DAY)",
            "%s",
            [row.get("completed_at") or row.get("updated_at"), row.get("completed_at") or row.get("updated_at")],
        )
        after_count = _negative_topic_count(
            cursor,
            business_id,
            row["topic"],
            "%s",
            "NOW()",
            [row.get("completed_at") or row.get("updated_at")],
        )
        reduction = round(((before_count - after_count) / before_count) * 100) if before_count else 100
        stories.append({
            "title": f"{row['topic'].title()} improved",
            "message": f"Negative mentions reduced by {max(reduction, 0)}% after completion.",
        })
    return stories


def _action_analytics(rows):
    total = len(rows)
    completed = len([row for row in rows if row["status"] in {"completed", "verified"}])
    ignored = len([row for row in rows if row["status"] == "ignored"])
    open_count = len([row for row in rows if row["status"] == "open"])
    in_progress = len([row for row in rows if row["status"] == "in_progress"])
    verified = len([row for row in rows if row["status"] == "verified"])
    return {
        "total": total,
        "open": open_count,
        "in_progress": in_progress,
        "completed": completed,
        "ignored": ignored,
        "verified": verified,
        "completion_rate": round((completed / total) * 100) if total else 0,
    }


def _verification_badge(row):
    if row["status"] == "completed":
        return "Awaiting Review Confirmation"
    if row["status"] == "verified":
        return "Verified Improvement"
    if row.get("is_reopened"):
        return "Issue Reappeared"
    return None


def _event_type_for_status(status):
    return {
        "in_progress": "started",
        "completed": "completed",
        "ignored": "ignored",
        "verified": "verified",
        "open": "reopened",
    }.get(status, "updated")


def _log_event(cursor, action_id, business_id, event_type, event_note=None):
    cursor.execute(
        """
        INSERT INTO consultant_action_events
        (action_id, business_id, event_type, event_note)
        VALUES (%s,%s,%s,%s)
        """,
        (action_id, business_id, event_type, event_note)
    )


def _issue_title(topic):
    return f"Improve {topic}"


def _normalize_topic(value):
    return str(value or "general experience").strip().lower()
