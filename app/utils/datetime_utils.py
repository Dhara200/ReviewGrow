from datetime import datetime, timezone
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")


def format_datetime_ist(value):
    """Format a UTC database datetime for display in Indian Standard Time.

    MySQL connector returns TIMESTAMP values as naive ``datetime`` objects.
    ReviewGrow treats those naive database values as UTC for backward
    compatibility; aware values retain their declared timezone before conversion.
    """
    if not isinstance(value, datetime):
        return "-"

    try:
        source = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return source.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")
    except (OverflowError, ValueError, OSError):
        return "-"


def register_datetime_filters(app):
    app.jinja_env.filters["datetime_ist"] = format_datetime_ist
