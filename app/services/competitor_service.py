from datetime import datetime, timedelta, timezone
import hashlib

from app.config import Config
from app.services.database_service import get_connection


def list_competitors(business_id):
    connection = get_connection(); cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM business_competitors WHERE business_id=%s AND is_active=TRUE ORDER BY user_rating_count DESC,competitor_name", (business_id,))
        return cursor.fetchall()
    finally: cursor.close(); connection.close()


def add_competitor(business_id, details):
    connection = get_connection(); cursor = connection.cursor(dictionary=True)
    try:
        connection.start_transaction()
        cursor.execute("SELECT COUNT(*) count FROM business_competitors WHERE business_id=%s AND is_active=TRUE FOR UPDATE", (business_id,))
        if int((cursor.fetchone() or {}).get("count") or 0) >= Config.COMPETITOR_MAX_TRACKED: raise ValueError(f"You can track up to {Config.COMPETITOR_MAX_TRACKED} competitors.")
        cursor.execute("""INSERT INTO business_competitors (business_id,google_place_id,competitor_name,formatted_address,latitude,longitude,primary_type,google_maps_url,rating,user_rating_count,business_status,distance_meters,last_refreshed_at,is_active)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(),TRUE)
          ON DUPLICATE KEY UPDATE competitor_name=VALUES(competitor_name),formatted_address=VALUES(formatted_address),latitude=VALUES(latitude),longitude=VALUES(longitude),primary_type=VALUES(primary_type),google_maps_url=VALUES(google_maps_url),rating=VALUES(rating),user_rating_count=VALUES(user_rating_count),business_status=VALUES(business_status),distance_meters=VALUES(distance_meters),last_refreshed_at=UTC_TIMESTAMP(),is_active=TRUE""",
          (business_id, details["google_place_id"], details["name"], details["formatted_address"], details["latitude"], details["longitude"], details["primary_type"], details["google_maps_url"], details["rating"], details["user_rating_count"], details["business_status"], details["distance_meters"]))
        connection.commit(); return True
    except: connection.rollback(); raise
    finally: cursor.close(); connection.close()


def remove_competitor(business_id, competitor_id):
    connection = get_connection(); cursor = connection.cursor()
    try:
        cursor.execute("UPDATE business_competitors SET is_active=FALSE WHERE id=%s AND business_id=%s AND is_active=TRUE", (competitor_id, business_id)); connection.commit(); return cursor.rowcount == 1
    finally: cursor.close(); connection.close()


def get_competitor(business_id, competitor_id):
    connection = get_connection(); cursor = connection.cursor(dictionary=True)
    try: cursor.execute("SELECT * FROM business_competitors WHERE id=%s AND business_id=%s AND is_active=TRUE", (competitor_id, business_id)); return cursor.fetchone()
    finally: cursor.close(); connection.close()


def refresh_allowed(competitor, now=None):
    refreshed = competitor.get("last_refreshed_at")
    if not refreshed: return True
    now = (now or datetime.now(timezone.utc)).replace(tzinfo=None)
    return refreshed <= now - timedelta(hours=Config.COMPETITOR_REFRESH_HOURS)


def update_competitor(business_id, competitor_id, details):
    connection = get_connection(); cursor = connection.cursor()
    try:
        cursor.execute("""UPDATE business_competitors SET competitor_name=%s,formatted_address=%s,latitude=%s,longitude=%s,primary_type=%s,google_maps_url=%s,rating=%s,user_rating_count=%s,business_status=%s,distance_meters=%s,last_refreshed_at=UTC_TIMESTAMP() WHERE id=%s AND business_id=%s AND is_active=TRUE""",
          (details["name"],details["formatted_address"],details["latitude"],details["longitude"],details["primary_type"],details["google_maps_url"],details["rating"],details["user_rating_count"],details["business_status"],details["distance_meters"],competitor_id,business_id)); connection.commit(); return cursor.rowcount == 1
    finally: cursor.close(); connection.close()


def comparison_summary(customer_rating, customer_review_count, competitors):
    rated = [item for item in competitors if item.get("rating") is not None]; counted = [item for item in competitors if item.get("user_rating_count") is not None]
    average_rating = round(sum(float(item["rating"]) for item in rated) / len(rated), 2) if rated else None
    average_count = round(sum(int(item["user_rating_count"]) for item in counted) / len(counted), 1) if counted else None
    rank = None
    if customer_rating is not None:
        rank = 1 + sum(1 for item in rated if float(item["rating"]) > float(customer_rating))
    return {"average_rating": average_rating, "average_review_count": average_count,
            "rating_gap": round(float(customer_rating)-average_rating, 2) if customer_rating is not None and average_rating is not None else None,
            "review_count_gap": round(float(customer_review_count)-average_count, 1) if customer_review_count is not None and average_count is not None else None,
            "highest_rated": max(rated, key=lambda item: float(item["rating"]), default=None),
            "most_reviewed": max(counted, key=lambda item: int(item["user_rating_count"]), default=None), "rank": rank, "rank_total": len(rated)+1 if rank else None}


def allow_competitor_search(user_id, business_id, limit=10, window_minutes=15):
    """Durable per-user/business search limit; browser session resets cannot bypass it."""
    key_hash = hashlib.sha256(f"{int(user_id)}:{int(business_id)}".encode()).hexdigest()
    connection = get_connection(); cursor = connection.cursor(dictionary=True)
    try:
        connection.start_transaction()
        cursor.execute("SELECT window_started_at,attempt_count FROM rate_limit_counters WHERE scope='competitor_search' AND key_hash=%s FOR UPDATE", (key_hash,))
        row = cursor.fetchone(); now = datetime.now(timezone.utc).replace(tzinfo=None); window_start = now - timedelta(minutes=window_minutes)
        if not row:
            cursor.execute("INSERT INTO rate_limit_counters (scope,key_hash,window_started_at,attempt_count) VALUES ('competitor_search',%s,%s,1)", (key_hash, now)); allowed = True
        elif row["window_started_at"] < window_start:
            cursor.execute("UPDATE rate_limit_counters SET window_started_at=%s,attempt_count=1,blocked_until=NULL WHERE scope='competitor_search' AND key_hash=%s", (now,key_hash)); allowed = True
        elif int(row["attempt_count"] or 0) >= limit: allowed = False
        else:
            cursor.execute("UPDATE rate_limit_counters SET attempt_count=attempt_count+1 WHERE scope='competitor_search' AND key_hash=%s", (key_hash,)); allowed = True
        connection.commit(); return allowed
    except: connection.rollback(); raise
    finally: cursor.close(); connection.close()
