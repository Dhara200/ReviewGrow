import json
import logging
import random
from datetime import date, datetime, timedelta, timezone

import mysql.connector
import requests

from app.config import Config
from app.services.database_service import get_connection
from app.services.google_places_service import (
    PlacesConfigurationError,
    PlacesPermissionError,
    PlacesTemporaryError,
    get_place_details,
)


logger = logging.getLogger(__name__)
JOB_TYPE = "competitor_refresh_all"
MAX_ATTEMPTS = 3


def create_competitor_refresh_job(user_id, business_id, now=None):
    """Create one durable job per business/configured refresh window."""
    now = _naive_utc(now)
    window_hours = max(1, int(Config.COMPETITOR_REFRESH_HOURS))
    window_number = int(now.replace(tzinfo=timezone.utc).timestamp()) // (window_hours * 3600)
    window_key = f"competitor-refresh-all:{int(business_id)}:{window_number}"
    active_key = f"competitor-refresh-all:{int(business_id)}"
    connection = get_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        connection.start_transaction()
        cursor.execute(
            "SELECT id FROM businesses WHERE id=%s FOR UPDATE", (business_id,)
        )
        if not cursor.fetchone():
            raise ValueError("Business was not found.")
        cursor.execute(
            "SELECT COUNT(*) AS count FROM business_competitors "
            "WHERE business_id=%s AND is_active=TRUE", (business_id,)
        )
        if int((cursor.fetchone() or {}).get("count") or 0) == 0:
            raise ValueError("Add at least one competitor before refreshing.")
        cursor.execute(
            """SELECT id FROM analysis_jobs
               WHERE business_id=%s AND job_type=%s
                 AND status='completed'
                 AND completed_at>=DATE_SUB(UTC_TIMESTAMP(6), INTERVAL %s HOUR)
               ORDER BY completed_at DESC LIMIT 1""",
            (business_id, JOB_TYPE, window_hours),
        )
        recent = cursor.fetchone()
        if recent:
            connection.commit()
            return recent["id"], False
        cursor.execute(
            "SELECT google_place_id,latitude,longitude FROM google_business_connections "
            "WHERE business_id=%s AND is_connected=TRUE ORDER BY connected_at DESC LIMIT 1",
            (business_id,),
        )
        source = cursor.fetchone() or {}
        if source.get("latitude") is None or source.get("longitude") is None:
            raise ValueError("Verified business location coordinates are required.")
        if not source.get("google_place_id"):
            raise ValueError("A verified Google Place ID is required.")
        cursor.execute(
            """INSERT INTO analysis_jobs
               (user_id,business_id,job_type,operation_key,refresh_window_key,
                active_operation_key,status,max_attempts,total_reviews)
               VALUES (%s,%s,%s,%s,%s,%s,'pending',%s,0)""",
            (user_id, business_id, JOB_TYPE, window_key, window_key,
             active_key, MAX_ATTEMPTS),
        )
        job_id = cursor.lastrowid
        connection.commit()
        return job_id, True
    except mysql.connector.IntegrityError as error:
        connection.rollback()
        if getattr(error, "errno", None) != 1062:
            raise
        cursor.execute(
            """SELECT id FROM analysis_jobs
               WHERE refresh_window_key=%s OR
                     (active_operation_key=%s AND status IN ('pending','processing'))
               ORDER BY id DESC LIMIT 1""",
            (window_key, active_key),
        )
        existing = cursor.fetchone()
        if not existing:
            raise
        return existing["id"], False
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def execute_competitor_refresh_job(job, ownership_check=None):
    """Refresh targets independently; successful targets survive partial failures."""
    business_id = int(job["business_id"])
    source, competitors = _load_targets(business_id)
    result = {
        "outcome": "failed", "total_targets": 1 + len(competitors),
        "customer_refreshed": False, "competitors_refreshed": 0,
        "competitors_skipped": 0, "competitors_failed": 0,
        "snapshot_rows_created": 0, "snapshot_rows_reused": 0,
        "failure_summaries": [],
    }
    if not source.get("google_place_id"):
        raise PlacesConfigurationError("Customer Google Place ID is unavailable.")
    if source.get("latitude") is None or source.get("longitude") is None:
        raise PlacesConfigurationError("Customer location coordinates are unavailable.")

    def still_owned():
        return ownership_check is None or ownership_check()

    if not still_owned():
        raise RuntimeError("Refresh job ownership was lost.")
    encountered = []
    try:
        details = get_place_details(source["google_place_id"], source=source)
        created = _persist_customer_snapshot(business_id, source, details, job["id"])
        result["customer_refreshed"] = True
        result["snapshot_rows_created" if created else "snapshot_rows_reused"] += 1
    except Exception as error:
        encountered.append(error)
        result["failure_summaries"].append({"target": "customer", "reason": _safe_reason(error)})

    for competitor in competitors:
        if not still_owned():
            raise RuntimeError("Refresh job ownership was lost.")
        try:
            details = get_place_details(competitor["google_place_id"], source=source)
            if details["google_place_id"] != competitor["google_place_id"]:
                raise ValueError("Google Place ID did not match the tracked competitor.")
            created = _persist_competitor_snapshot(business_id, competitor, details, job["id"])
            result["competitors_refreshed"] += 1
            result["snapshot_rows_created" if created else "snapshot_rows_reused"] += 1
        except Exception as error:
            encountered.append(error)
            result["competitors_failed"] += 1
            result["failure_summaries"].append({
                "target": f"competitor:{competitor['id']}", "reason": _safe_reason(error)
            })

    successes = int(result["customer_refreshed"]) + result["competitors_refreshed"]
    failures = len(result["failure_summaries"])
    if not successes and encountered and all(is_retryable_refresh_error(error) for error in encountered):
        raise encountered[0]
    result["outcome"] = "completed" if successes and not failures else "partially_completed" if successes else "failed"
    result["failure_summaries"] = result["failure_summaries"][:10]
    return result


def complete_competitor_refresh_job(job_id, worker_id, result):
    connection = get_connection(); cursor = connection.cursor()
    try:
        cursor.execute(
            """UPDATE analysis_jobs SET status='completed',completed_at=UTC_TIMESTAMP(6),
               result_json=%s,active_operation_key=NULL,worker_id=NULL,
               lease_expires_at=NULL,heartbeat_at=NULL,error_message=NULL
               WHERE id=%s AND status='processing' AND worker_id=%s
                 AND lease_expires_at>UTC_TIMESTAMP(6)""",
            (json.dumps(result, separators=(",", ":")), job_id, worker_id),
        )
        connection.commit(); return cursor.rowcount == 1
    finally:
        cursor.close(); connection.close()


def enqueue_due_competitor_refresh_jobs(limit=20):
    """Idempotent scheduler scan; unique refresh-window keys arbitrate replicas."""
    connection = get_connection(); cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """SELECT b.id AS business_id,b.user_id
               FROM businesses b
               JOIN google_business_connections g ON g.business_id=b.id AND g.is_connected=TRUE
               JOIN business_competitors c ON c.business_id=b.id AND c.is_active=TRUE
               WHERE g.google_place_id IS NOT NULL AND g.latitude IS NOT NULL AND g.longitude IS NOT NULL
               GROUP BY b.id,b.user_id
               HAVING MIN(COALESCE(c.last_refreshed_at,'1970-01-01'))
                        < DATE_SUB(UTC_TIMESTAMP(6), INTERVAL %s HOUR)
               ORDER BY b.id LIMIT %s""",
            (int(Config.COMPETITOR_REFRESH_HOURS), int(limit)),
        )
        eligible = cursor.fetchall()
    finally:
        cursor.close(); connection.close()
    created = reused = 0
    for row in eligible:
        try:
            _, was_created = create_competitor_refresh_job(row["user_id"], row["business_id"])
            created += int(was_created); reused += int(not was_created)
        except ValueError:
            continue
    return {"eligible": len(eligible), "created": created, "reused": reused}


def get_history_readiness(business_id):
    competitors = []
    connection = get_connection(); cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id FROM business_competitors WHERE business_id=%s AND is_active=TRUE", (business_id,))
        competitors = [int(row["id"]) for row in cursor.fetchall()]
        cursor.execute(
            """SELECT subject_key,COUNT(*) snapshot_count,MIN(captured_at) earliest,
                      MAX(captured_at) latest,MIN(capture_date) earliest_date,MAX(capture_date) latest_date
               FROM business_reputation_snapshots WHERE business_id=%s GROUP BY subject_key""",
            (business_id,),
        )
        rows = cursor.fetchall()
    finally:
        cursor.close(); connection.close()
    by_key = {row["subject_key"]: row for row in rows}
    required = ["customer", *[f"competitor:{item}" for item in competitors]]
    missing = [key for key in required if key not in by_key]
    counts = [int(by_key[key]["snapshot_count"]) for key in required if key in by_key]
    all_dates = [row[edge] for row in by_key.values() for edge in ("earliest_date", "latest_date") if row.get(edge)]
    earliest, latest = (min(all_dates), max(all_dates)) if all_dates else (None, None)
    span = (latest - earliest).days if earliest and latest else 0
    separated = all(
        row.get("earliest") and row.get("latest") and
        (row["latest"] - row["earliest"]).total_seconds() >= 86400
        for key in required if (row := by_key.get(key))
    ) and not missing
    minimum = min(counts) if counts and not missing else 0
    next_expected = datetime.now(timezone.utc) + timedelta(hours=Config.COMPETITOR_REFRESH_HOURS)
    return {
        "customer_snapshot_count": int(by_key.get("customer", {}).get("snapshot_count") or 0),
        "minimum_competitor_snapshot_count": min([int(by_key.get(f'competitor:{item}', {}).get('snapshot_count') or 0) for item in competitors], default=0),
        "earliest_snapshot_date": earliest, "latest_snapshot_date": latest,
        "history_days": span, "enough_for_basic_change": minimum >= 2 and separated,
        "enough_for_trend_chart": minimum >= 7 and span >= 6 and not missing,
        "enough_for_monthly_trend": minimum >= 30 and span >= 29 and not missing,
        "missing_subjects": missing, "next_expected_refresh": next_expected,
    }


def get_competitor_history_foundation(business_id):
    """Business-scoped, JSON-safe daily series reserved for the future chart phase."""
    connection = get_connection(); cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """SELECT s.capture_date,s.subject_type,s.competitor_id,s.subject_name,
                      s.rating,s.user_rating_count
               FROM business_reputation_snapshots s
               LEFT JOIN business_competitors c ON c.id=s.competitor_id
               WHERE s.business_id=%s
                 AND (s.subject_type='customer' OR (c.business_id=%s AND c.is_active=TRUE))
               ORDER BY s.capture_date,s.subject_type,s.competitor_id""",
            (business_id, business_id),
        )
        rows = cursor.fetchall()
    finally:
        cursor.close(); connection.close()
    dates = sorted({row["capture_date"] for row in rows})
    subjects = {}
    for row in rows:
        key = "customer" if row["subject_type"] == "customer" else f"competitor:{row['competitor_id']}"
        subjects.setdefault(key, {"name": row["subject_name"], "values": {}})["values"][row["capture_date"]] = row
    series = []
    for key, subject in subjects.items():
        series.append({
            "subject_key": key, "name": subject["name"],
            "rating": [float(subject["values"][day]["rating"]) if day in subject["values"] and subject["values"][day]["rating"] is not None else None for day in dates],
            "review_count": [int(subject["values"][day]["user_rating_count"]) if day in subject["values"] and subject["values"][day]["user_rating_count"] is not None else None for day in dates],
        })
    return {"dates": [day.isoformat() for day in dates], "subjects": series}


def _load_targets(business_id):
    connection = get_connection(); cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """SELECT b.business_name AS name,b.business_type,g.google_place_id,
                      g.latitude,g.longitude,g.primary_category,g.formatted_address
               FROM businesses b JOIN google_business_connections g ON g.business_id=b.id
               WHERE b.id=%s AND g.is_connected=TRUE ORDER BY g.connected_at DESC LIMIT 1""",
            (business_id,),
        )
        source = cursor.fetchone() or {}
        cursor.execute("SELECT * FROM business_competitors WHERE business_id=%s AND is_active=TRUE ORDER BY id", (business_id,))
        return source, cursor.fetchall()
    finally:
        cursor.close(); connection.close()


def _persist_customer_snapshot(business_id, source, details, job_id):
    connection = get_connection(); cursor = connection.cursor()
    try:
        connection.start_transaction()
        cursor.execute(
            """UPDATE google_business_connections SET google_location_name=%s,
               formatted_address=%s,latitude=%s,longitude=%s,primary_category=%s
               WHERE business_id=%s AND is_connected=TRUE""",
            (details["name"], details["formatted_address"], details["latitude"],
             details["longitude"], details["primary_type"], business_id),
        )
        created = _upsert_snapshot(cursor, business_id, "customer", "customer", None, details, job_id)
        connection.commit(); return created
    except Exception:
        connection.rollback(); raise
    finally:
        cursor.close(); connection.close()


def _persist_competitor_snapshot(business_id, competitor, details, job_id):
    connection = get_connection(); cursor = connection.cursor()
    try:
        connection.start_transaction()
        cursor.execute(
            """UPDATE business_competitors SET competitor_name=%s,formatted_address=%s,
               latitude=%s,longitude=%s,primary_type=%s,google_maps_url=%s,rating=%s,
               user_rating_count=%s,business_status=%s,distance_meters=%s,
               last_refreshed_at=UTC_TIMESTAMP(6)
               WHERE id=%s AND business_id=%s AND is_active=TRUE""",
            (details["name"], details["formatted_address"], details["latitude"], details["longitude"],
             details["primary_type"], details["google_maps_url"], details["rating"],
             details["user_rating_count"], details["business_status"], details["distance_meters"],
             competitor["id"], business_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Tracked competitor is no longer active.")
        created = _upsert_snapshot(cursor, business_id, "competitor", f"competitor:{competitor['id']}", competitor["id"], details, job_id)
        connection.commit(); return created
    except Exception:
        connection.rollback(); raise
    finally:
        cursor.close(); connection.close()


def _upsert_snapshot(cursor, business_id, subject_type, subject_key, competitor_id, details, job_id):
    cursor.execute(
        """INSERT INTO business_reputation_snapshots
           (business_id,subject_type,subject_key,competitor_id,google_place_id,subject_name,
            rating,user_rating_count,business_status,captured_at,capture_date,source,refresh_job_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(6),UTC_DATE(),'google_places',%s)
           ON DUPLICATE KEY UPDATE google_place_id=VALUES(google_place_id),
             subject_name=VALUES(subject_name),rating=VALUES(rating),
             user_rating_count=VALUES(user_rating_count),business_status=VALUES(business_status),
             captured_at=VALUES(captured_at),refresh_job_id=VALUES(refresh_job_id)""",
        (business_id, subject_type, subject_key, competitor_id, details["google_place_id"],
         details["name"], details["rating"], details["user_rating_count"],
         details["business_status"], job_id),
    )
    return cursor.rowcount == 1


def _safe_reason(error):
    if isinstance(error, PlacesTemporaryError): return "Google Places was temporarily unavailable."
    if isinstance(error, PlacesPermissionError): return "Google Places access is not configured correctly."
    if isinstance(error, PlacesConfigurationError): return str(error)[:200]
    return "The target could not be refreshed."


def is_retryable_refresh_error(error):
    return isinstance(error, (PlacesTemporaryError, requests.Timeout, requests.ConnectionError))


def retry_delay_seconds(attempt):
    return min(300, (2 ** max(int(attempt) - 1, 0)) * 15 + random.randint(0, 5))


def _naive_utc(value=None):
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value
