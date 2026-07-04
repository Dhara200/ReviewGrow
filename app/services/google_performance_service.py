import logging
from datetime import date, datetime, timedelta

from app.services.google_business_service import GoogleBusinessError, api_get


PERFORMANCE_BASE_URL = "https://businessprofileperformance.googleapis.com/v1"
logger = logging.getLogger(__name__)


PERFORMANCE_METRICS = {
    "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH": "Desktop Search views",
    "BUSINESS_IMPRESSIONS_MOBILE_SEARCH": "Mobile Search views",
    "BUSINESS_IMPRESSIONS_DESKTOP_MAPS": "Desktop Maps views",
    "BUSINESS_IMPRESSIONS_MOBILE_MAPS": "Mobile Maps views",
    "WEBSITE_CLICKS": "Website clicks",
    "CALL_CLICKS": "Phone calls",
    "BUSINESS_DIRECTION_REQUESTS": "Direction requests",
    "BUSINESS_BOOKINGS": "Bookings",
    "BUSINESS_FOOD_ORDERS": "Food orders",
    "BUSINESS_CONVERSATIONS": "Messages",
}


def default_performance_range():
    end_date = date.today()
    start_date = end_date - timedelta(days=30)

    return start_date, end_date


def parse_date_range(start_value=None, end_value=None):
    default_start, default_end = default_performance_range()

    try:
        start_date = datetime.strptime(start_value, "%Y-%m-%d").date() if start_value else default_start
        end_date = datetime.strptime(end_value, "%Y-%m-%d").date() if end_value else default_end
    except ValueError as exc:
        raise GoogleBusinessError("Invalid date range. Please choose valid start and end dates.") from exc

    if start_date > end_date:
        raise GoogleBusinessError("Invalid date range. Start date must be before end date.")

    if (end_date - start_date).days > 366:
        raise GoogleBusinessError("Date range is too large. Please choose 366 days or less.")

    return start_date, end_date


def performance_location_name(location_id):
    if not location_id:
        raise GoogleBusinessError("Google location is missing. Please reconnect Google Business Profile.")

    if location_id.startswith("accounts/") and "/locations/" in location_id:
        return f"locations/{location_id.rsplit('/locations/', 1)[1]}"

    if location_id.startswith("locations/"):
        return location_id

    return f"locations/{location_id}"


def _date_params(prefix, value):
    return {
        f"{prefix}.year": value.year,
        f"{prefix}.month": value.month,
        f"{prefix}.day": value.day,
    }


def _google_date_to_python(value):
    if not value:
        return None

    try:
        return date(
            int(value.get("year")),
            int(value.get("month")),
            int(value.get("day")),
        )
    except (TypeError, ValueError):
        return None


def _metric_entries_from_response(data):
    entries = []

    for group in data.get("multiDailyMetricTimeSeries", []):
        series_items = group.get("dailyMetricTimeSeries") or []

        if isinstance(series_items, dict):
            series_items = [series_items]

        for series in series_items:
            metric_name = series.get("dailyMetric") or group.get("dailyMetric")
            time_series = series.get("timeSeries") or {}

            for point in time_series.get("datedValues", []):
                metric_date = _google_date_to_python(point.get("date"))

                if not metric_name or not metric_date:
                    continue

                entries.append({
                    "metric_name": metric_name,
                    "metric_value": int(point.get("value", 0)),
                    "metric_date": metric_date,
                })

    return entries


def fetch_performance_metrics(access_token, google_location_id, start_date, end_date):
    location = performance_location_name(google_location_id)
    params = {
        "dailyMetrics": list(PERFORMANCE_METRICS.keys()),
        **_date_params("dailyRange.start_date", start_date),
        **_date_params("dailyRange.end_date", end_date),
    }

    data = api_get(
        access_token,
        f"{PERFORMANCE_BASE_URL}/{location}:fetchMultiDailyMetricsTimeSeries",
        params=params,
    )

    return _metric_entries_from_response(data)


def save_performance_metrics(cursor, connection, entries, start_date, end_date):
    cursor.execute(
        """
        DELETE FROM google_business_performance
        WHERE user_id=%s
        AND business_id=%s
        AND google_location_id=%s
        AND period_start=%s
        AND period_end=%s
        """,
        (
            connection["user_id"],
            connection["business_id"],
            connection["google_location_id"],
            start_date,
            end_date,
        )
    )

    for entry in entries:
        cursor.execute(
            """
            INSERT INTO google_business_performance
            (
                user_id,
                business_id,
                google_location_id,
                metric_name,
                metric_value,
                metric_date,
                period_start,
                period_end
            )
            VALUES
            (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                connection["user_id"],
                connection["business_id"],
                connection["google_location_id"],
                entry["metric_name"],
                entry["metric_value"],
                entry["metric_date"],
                start_date,
                end_date,
            )
        )

    return len(entries)


def sync_performance_metrics(cursor, connection, start_date, end_date):
    entries = fetch_performance_metrics(
        connection["access_token"],
        connection["google_location_id"],
        start_date,
        end_date,
    )
    saved_count = save_performance_metrics(cursor, connection, entries, start_date, end_date)

    return {
        "fetched_count": len(entries),
        "saved_count": saved_count,
    }


def empty_summary():
    return {
        "business_profile_views": None,
        "search_views": None,
        "maps_views": None,
        "website_clicks": None,
        "phone_calls": None,
        "direction_requests": None,
        "bookings": None,
        "food_orders": None,
        "messages": None,
    }


def load_performance_data(cursor, business_id, user_id, start_date, end_date):
    cursor.execute(
        """
        SELECT
            metric_name,
            metric_value,
            metric_date
        FROM google_business_performance
        WHERE business_id=%s
        AND user_id=%s
        AND metric_date BETWEEN %s AND %s
        ORDER BY metric_date DESC, metric_name ASC
        """,
        (business_id, user_id, start_date, end_date)
    )
    rows = cursor.fetchall()

    cursor.execute(
        """
        SELECT MAX(created_at) AS last_synced_at
        FROM google_business_performance
        WHERE business_id=%s
        AND user_id=%s
        AND metric_date BETWEEN %s AND %s
        """,
        (business_id, user_id, start_date, end_date)
    )
    sync_row = cursor.fetchone() or {}

    totals = {}
    trend = {}
    last_synced_at = sync_row.get("last_synced_at")

    for row in rows:
        metric_name = row["metric_name"]
        metric_value = int(row["metric_value"] or 0)
        metric_date = row["metric_date"]
        totals[metric_name] = totals.get(metric_name, 0) + metric_value
        trend.setdefault(metric_date, {})[metric_name] = metric_value

    search_views = _sum_available(totals, [
        "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH",
        "BUSINESS_IMPRESSIONS_MOBILE_SEARCH",
    ])
    maps_views = _sum_available(totals, [
        "BUSINESS_IMPRESSIONS_DESKTOP_MAPS",
        "BUSINESS_IMPRESSIONS_MOBILE_MAPS",
    ])

    summary = empty_summary()
    summary.update({
        "search_views": search_views,
        "maps_views": maps_views,
        "business_profile_views": _combine_optional(search_views, maps_views),
        "website_clicks": totals.get("WEBSITE_CLICKS"),
        "phone_calls": totals.get("CALL_CLICKS"),
        "direction_requests": totals.get("BUSINESS_DIRECTION_REQUESTS"),
        "bookings": totals.get("BUSINESS_BOOKINGS"),
        "food_orders": totals.get("BUSINESS_FOOD_ORDERS"),
        "messages": totals.get("BUSINESS_CONVERSATIONS"),
    })

    trend_rows = []
    for metric_date, metric_values in sorted(trend.items(), reverse=True):
        date_search = _sum_available(metric_values, [
            "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH",
            "BUSINESS_IMPRESSIONS_MOBILE_SEARCH",
        ])
        date_maps = _sum_available(metric_values, [
            "BUSINESS_IMPRESSIONS_DESKTOP_MAPS",
            "BUSINESS_IMPRESSIONS_MOBILE_MAPS",
        ])

        trend_rows.append({
            "metric_date": metric_date,
            "search_views": date_search,
            "maps_views": date_maps,
            "website_clicks": metric_values.get("WEBSITE_CLICKS"),
            "phone_calls": metric_values.get("CALL_CLICKS"),
            "direction_requests": metric_values.get("BUSINESS_DIRECTION_REQUESTS"),
        })

    chart_data = [
        {
            "date": str(row["metric_date"]),
            "search_views": row["search_views"] or 0,
            "maps_views": row["maps_views"] or 0,
            "website_clicks": row["website_clicks"] or 0,
            "phone_calls": row["phone_calls"] or 0,
            "direction_requests": row["direction_requests"] or 0,
        }
        for row in reversed(trend_rows)
    ]

    return {
        "summary": summary,
        "trend_rows": trend_rows,
        "chart_data": chart_data,
        "last_synced_at": last_synced_at,
    }


def _sum_available(values, metric_names):
    found = False
    total = 0

    for metric_name in metric_names:
        if metric_name in values:
            found = True
            total += int(values[metric_name] or 0)

    return total if found else None


def _combine_optional(*values):
    available = [value for value in values if value is not None]

    if not available:
        return None

    return sum(available)
