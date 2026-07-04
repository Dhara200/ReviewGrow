from datetime import datetime

from flask import (
    Blueprint,
    flash,
    render_template,
    request,
    session,
    redirect
)

from app.services.database_service import get_connection
from app.services.ai_usage_service import refresh_ai_monthly_usage
from app.services.subscription_service import approve_payment, reject_payment

admin_bp = Blueprint("admin", __name__)


def _money(value):
    return float(value or 0)


def _int_value(value):
    return int(value or 0)


def _month_starts(month_count=6):
    today = datetime.utcnow()
    year = today.year
    month = today.month
    starts = []

    for offset in range(month_count - 1, -1, -1):
        month_index = month - offset
        item_year = year

        while month_index <= 0:
            month_index += 12
            item_year -= 1

        starts.append(datetime(item_year, month_index, 1))

    return starts


def _admin_required():
    if "user_id" not in session:
        return redirect("/login-page")

    if session.get("role") != "admin":
        return "Access Denied", 403

    return None


def _selected_month():
    month_value = request.args.get("month") or datetime.utcnow().strftime("%Y-%m")

    try:
        month_start = datetime.strptime(month_value, "%Y-%m")
    except ValueError:
        month_start = datetime.utcnow().replace(day=1)
        month_value = month_start.strftime("%Y-%m")

    next_month = month_start.replace(
        year=month_start.year + 1,
        month=1
    ) if month_start.month == 12 else month_start.replace(
        month=month_start.month + 1
    )

    return month_value, month_start, next_month


def _ai_filter_args():
    return {
        "month": request.args.get("month") or datetime.utcnow().strftime("%Y-%m"),
        "user_id": request.args.get("user_id", type=int),
        "business_id": request.args.get("business_id", type=int),
        "provider": (request.args.get("provider") or "").strip(),
        "model": (request.args.get("model") or "").strip(),
    }


def _add_ai_filters(filters, params, include_user=True, include_business=True, alias="l"):
    clauses = []

    if include_user and filters.get("user_id"):
        clauses.append(f"{alias}.user_id=%s")
        params.append(filters["user_id"])

    if include_business and filters.get("business_id"):
        clauses.append(f"{alias}.business_id=%s")
        params.append(filters["business_id"])

    if filters.get("provider"):
        clauses.append(f"{alias}.provider=%s")
        params.append(filters["provider"])

    if filters.get("model"):
        clauses.append(f"{alias}.model_name=%s")
        params.append(filters["model"])

    return clauses


def _query_filter_options(cursor):
    cursor.execute("SELECT id, name, email FROM users ORDER BY name ASC, email ASC")
    users = cursor.fetchall()

    cursor.execute(
        """
        SELECT id, user_id, business_name
        FROM businesses
        ORDER BY business_name ASC
        """
    )
    businesses = cursor.fetchall()

    cursor.execute(
        """
        SELECT DISTINCT provider
        FROM ai_usage_logs
        WHERE provider IS NOT NULL
        ORDER BY provider ASC
        """
    )
    providers = [row["provider"] for row in cursor.fetchall()]

    cursor.execute(
        """
        SELECT DISTINCT model_name
        FROM ai_usage_logs
        WHERE model_name IS NOT NULL
        ORDER BY model_name ASC
        """
    )
    models = [row["model_name"] for row in cursor.fetchall()]

    return {
        "users": users,
        "businesses": businesses,
        "providers": providers,
        "models": models,
    }


@admin_bp.route("/admin/dashboard")
def admin_dashboard():

    guard = _admin_required()
    if guard:
        return guard

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT COUNT(*) AS total_users FROM users")
    total_users = cursor.fetchone()["total_users"]

    cursor.execute("SELECT COUNT(*) AS total_businesses FROM businesses")
    total_businesses = cursor.fetchone()["total_businesses"]

    cursor.execute("SELECT COUNT(*) AS total_reviews FROM reviews")
    total_reviews = cursor.fetchone()["total_reviews"]

    cursor.execute("SELECT COUNT(*) AS total_reports FROM reports")
    total_reports = cursor.fetchone()["total_reports"]

    cursor.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total_income
        FROM payments
        WHERE payment_status='success'
        """
    )
    total_income = _money(cursor.fetchone()["total_income"])

    cursor.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS monthly_revenue
        FROM payments
        WHERE payment_status='success'
        AND YEAR(COALESCE(paid_at, created_at)) = YEAR(CURRENT_DATE())
        AND MONTH(COALESCE(paid_at, created_at)) = MONTH(CURRENT_DATE())
        """
    )
    monthly_revenue = _money(cursor.fetchone()["monthly_revenue"])
    annual_recurring_revenue = monthly_revenue * 12

    cursor.execute(
        """
        SELECT COUNT(*) AS pending_payment_count
        FROM payments
        WHERE payment_status='pending'
        """
    )
    pending_payment_count = cursor.fetchone()["pending_payment_count"]

    month_starts = _month_starts(6)
    trend_start = month_starts[0]
    cursor.execute(
        """
        SELECT
            DATE_FORMAT(COALESCE(paid_at, created_at), '%Y-%m') AS revenue_month,
            COALESCE(SUM(amount), 0) AS revenue,
            COUNT(*) AS successful_payment_count
        FROM payments
        WHERE payment_status='success'
        AND COALESCE(paid_at, created_at) >= %s
        GROUP BY revenue_month
        ORDER BY revenue_month ASC
        """,
        (trend_start,)
    )
    revenue_rows = {
        row["revenue_month"]: row
        for row in cursor.fetchall()
    }
    revenue_trend = []

    for month_start in month_starts:
        key = month_start.strftime("%Y-%m")
        row = revenue_rows.get(key, {})
        revenue_trend.append({
            "month_name": month_start.strftime("%b %Y"),
            "revenue": _money(row.get("revenue")),
            "successful_payment_count": row.get("successful_payment_count", 0),
        })

    max_trend_revenue = max(
        [item["revenue"] for item in revenue_trend] or [0]
    )

    cursor.close()
    conn.close()
    
    return render_template(
    "admin_dashboard.html",
    total_users=total_users,
    total_businesses=total_businesses,
    total_reviews=total_reviews,
    total_reports=total_reports,
    total_income=total_income,
    monthly_revenue=monthly_revenue,
    annual_recurring_revenue=annual_recurring_revenue,
    pending_payment_count=pending_payment_count,
    revenue_trend=revenue_trend,
    max_trend_revenue=max_trend_revenue,
)


@admin_bp.route("/admin/ai-analysis")
def admin_ai_analysis():
    guard = _admin_required()
    if guard:
        return guard

    refresh_ai_monthly_usage()

    filters = _ai_filter_args()
    month_value, month_start, next_month = _selected_month()
    filters["month"] = month_value

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    options = _query_filter_options(cursor)

    monthly_params = [month_start, next_month]
    monthly_clauses = [
        "l.created_at >= %s",
        "l.created_at < %s",
    ]
    monthly_clauses.extend(_add_ai_filters(filters, monthly_params))
    monthly_where = " AND ".join(monthly_clauses)

    cursor.execute(
        f"""
        SELECT
            COUNT(*) AS total_requests,
            SUM(CASE WHEN l.request_status='success' THEN 1 ELSE 0 END) AS successful_requests,
            SUM(CASE WHEN l.request_status='failed' THEN 1 ELSE 0 END) AS failed_requests,
            COALESCE(SUM(l.total_tokens), 0) AS total_tokens,
            COALESCE(SUM(l.estimated_cost), 0) AS estimated_cost,
            COALESCE(AVG(l.response_time_ms), 0) AS average_response_time_ms
        FROM ai_usage_logs l
        WHERE {monthly_where}
        """,
        tuple(monthly_params)
    )
    summary = cursor.fetchone() or {}

    review_params = [month_start, next_month]
    review_clauses = [
        "r.analyzed_at >= %s",
        "r.analyzed_at < %s",
    ]
    if filters.get("user_id"):
        review_clauses.append("b.user_id=%s")
        review_params.append(filters["user_id"])
    if filters.get("business_id"):
        review_clauses.append("r.business_id=%s")
        review_params.append(filters["business_id"])

    cursor.execute(
        f"""
        SELECT COUNT(*) AS reviews_analyzed
        FROM reviews r
        JOIN businesses b
            ON b.id = r.business_id
        WHERE {" AND ".join(review_clauses)}
        """,
        tuple(review_params)
    )
    summary["reviews_analyzed"] = _int_value(
        (cursor.fetchone() or {}).get("reviews_analyzed")
    )

    lifetime_params = []
    lifetime_clauses = _add_ai_filters(
        filters,
        lifetime_params,
        include_user=False,
        alias="l"
    )
    lifetime_where = (
        "WHERE " + " AND ".join(lifetime_clauses)
        if lifetime_clauses else ""
    )

    month_user_params = [month_start, next_month]
    month_user_clauses = [
        "l.created_at >= %s",
        "l.created_at < %s",
    ]
    month_user_clauses.extend(_add_ai_filters(
        filters,
        month_user_params,
        include_user=False,
        alias="l"
    ))
    month_user_where = "WHERE " + " AND ".join(month_user_clauses)

    table_params = tuple(lifetime_params + month_user_params)
    cursor.execute(
        f"""
        SELECT
            u.id,
            u.name,
            u.email,
            COALESCE(bc.business_count, 0) AS business_count,
            COALESCE(total_usage.total_ai_requests, 0) AS total_ai_requests,
            COALESCE(month_usage.current_month_ai_requests, 0) AS current_month_ai_requests,
            COALESCE(month_usage.current_month_tokens, 0) AS current_month_tokens,
            COALESCE(month_usage.current_month_estimated_cost, 0) AS current_month_estimated_cost,
            COALESCE(month_usage.failed_requests, 0) AS failed_requests,
            total_usage.last_ai_activity
        FROM users u
        LEFT JOIN (
            SELECT user_id, COUNT(*) AS business_count
            FROM businesses
            GROUP BY user_id
        ) bc
            ON bc.user_id = u.id
        LEFT JOIN (
            SELECT
                l.user_id,
                COUNT(*) AS total_ai_requests,
                MAX(l.created_at) AS last_ai_activity
            FROM ai_usage_logs l
            {lifetime_where}
            GROUP BY l.user_id
        ) total_usage
            ON total_usage.user_id = u.id
        LEFT JOIN (
            SELECT
                l.user_id,
                COUNT(*) AS current_month_ai_requests,
                COALESCE(SUM(l.total_tokens), 0) AS current_month_tokens,
                COALESCE(SUM(l.estimated_cost), 0) AS current_month_estimated_cost,
                SUM(CASE WHEN l.request_status='failed' THEN 1 ELSE 0 END) AS failed_requests
            FROM ai_usage_logs l
            {month_user_where}
            GROUP BY l.user_id
        ) month_usage
            ON month_usage.user_id = u.id
        WHERE (%s IS NULL OR u.id=%s)
        ORDER BY month_usage.current_month_ai_requests DESC, total_usage.last_ai_activity DESC, u.created_at DESC
        """,
        table_params + (filters.get("user_id"), filters.get("user_id"))
    )
    user_rows = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        "admin_ai_analysis.html",
        filters=filters,
        options=options,
        summary=summary,
        user_rows=user_rows
    )


@admin_bp.route("/admin/users/<int:user_id>/ai-analysis")
def admin_user_ai_analysis(user_id):
    guard = _admin_required()
    if guard:
        return guard

    filters = _ai_filter_args()
    month_value, month_start, next_month = _selected_month()
    filters["month"] = month_value
    filters["user_id"] = user_id

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute(
        """
        SELECT id, name, email, role, created_at
        FROM users
        WHERE id=%s
        """,
        (user_id,)
    )
    user = cursor.fetchone()

    if not user:
        cursor.close()
        conn.close()
        flash("User was not found.", "danger")
        return redirect("/admin/ai-analysis")

    options = _query_filter_options(cursor)

    usage_params = [month_start, next_month, user_id]
    usage_clauses = [
        "l.created_at >= %s",
        "l.created_at < %s",
        "l.user_id=%s",
    ]
    if filters.get("business_id"):
        usage_clauses.append("l.business_id=%s")
        usage_params.append(filters["business_id"])
    if filters.get("provider"):
        usage_clauses.append("l.provider=%s")
        usage_params.append(filters["provider"])
    if filters.get("model"):
        usage_clauses.append("l.model_name=%s")
        usage_params.append(filters["model"])
    usage_where = "WHERE " + " AND ".join(usage_clauses)

    business_where = ["b.user_id=%s"]
    business_params = [user_id]
    if filters.get("business_id"):
        business_where.append("b.id=%s")
        business_params.append(filters["business_id"])

    reviews_params = [month_start, next_month]
    cursor.execute(
        f"""
        SELECT
            b.id,
            b.business_name,
            b.business_type,
            COALESCE(usage_rows.current_month_requests, 0) AS current_month_requests,
            COALESCE(usage_rows.current_month_tokens, 0) AS current_month_tokens,
            COALESCE(usage_rows.current_month_estimated_cost, 0) AS current_month_estimated_cost,
            COALESCE(reviews_rows.reviews_analyzed, 0) AS reviews_analyzed,
            COALESCE(usage_rows.failed_requests, 0) AS failed_requests,
            (
                SELECT j.status
                FROM analysis_jobs j
                WHERE j.business_id = b.id
                ORDER BY j.created_at DESC
                LIMIT 1
            ) AS latest_job_status,
            (
                SELECT MAX(j.completed_at)
                FROM analysis_jobs j
                WHERE j.business_id = b.id
            ) AS last_analysis_date
        FROM businesses b
        LEFT JOIN (
            SELECT
                l.business_id,
                COUNT(*) AS current_month_requests,
                COALESCE(SUM(l.total_tokens), 0) AS current_month_tokens,
                COALESCE(SUM(l.estimated_cost), 0) AS current_month_estimated_cost,
                SUM(CASE WHEN l.request_status='failed' THEN 1 ELSE 0 END) AS failed_requests
            FROM ai_usage_logs l
            {usage_where}
            GROUP BY l.business_id
        ) usage_rows
            ON usage_rows.business_id = b.id
        LEFT JOIN (
            SELECT business_id, COUNT(*) AS reviews_analyzed
            FROM reviews
            WHERE analyzed_at >= %s
            AND analyzed_at < %s
            GROUP BY business_id
        ) reviews_rows
            ON reviews_rows.business_id = b.id
        WHERE {" AND ".join(business_where)}
        ORDER BY usage_rows.current_month_requests DESC, b.created_at DESC
        """,
        tuple(usage_params + reviews_params + business_params)
    )
    businesses = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        "admin_user_ai_analysis.html",
        user=user,
        businesses=businesses,
        filters=filters,
        options=options
    )


@admin_bp.route("/admin/users")
def admin_users():
    guard = _admin_required()
    if guard:
        return guard

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
SELECT
u.id,
u.name,
u.email,
u.role,
u.created_at,
COUNT(b.id) AS business_count
FROM users u

LEFT JOIN businesses b
ON u.id=b.user_id

GROUP BY
u.id,
u.name,
u.email,
u.role,
u.created_at

ORDER BY u.created_at DESC
""")
    users = cursor.fetchall()
    cursor.close()
    conn.close()

    return render_template("admin_users.html", users=users)


@admin_bp.route("/admin/users/<int:user_id>")
def admin_user_detail(user_id):
    guard = _admin_required()
    if guard:
        return guard

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute(
        """
        SELECT id, name, email, role, created_at
        FROM users
        WHERE id=%s
        """,
        (user_id,)
    )
    user = cursor.fetchone()

    if not user:
        cursor.close()
        conn.close()
        flash("User was not found.", "danger")
        return redirect("/admin/users")

    cursor.execute(
        """
        SELECT
            plan_name,
            status,
            subscription_start_date,
            subscription_end_date,
            review_credits
        FROM subscriptions
        WHERE user_id=%s
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (user_id,)
    )
    subscription = cursor.fetchone()

    cursor.execute(
        """
        SELECT
            id,
            amount,
            currency,
            payment_method,
            payment_status,
            transaction_id,
            paid_at,
            created_at
        FROM payments
        WHERE user_id=%s
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (user_id,)
    )
    latest_payment = cursor.fetchone()

    cursor.execute(
        """
        SELECT
            id,
            amount,
            currency,
            payment_method,
            payment_status,
            transaction_id,
            created_at,
            paid_at
        FROM payments
        WHERE user_id=%s
        ORDER BY created_at DESC, id DESC
        """,
        (user_id,)
    )
    payments = cursor.fetchall()

    cursor.execute(
        """
        SELECT
            b.id,
            b.business_name,
            b.business_type,
            b.city,
            b.state,
            b.country,
            COUNT(DISTINCT r.id) AS review_count,
            COUNT(DISTINCT rp.id) AS report_count,
            MAX(
                CASE
                    WHEN gbc.is_connected = TRUE
                        OR gbc.access_token IS NOT NULL
                        OR gbc.google_account_id IS NOT NULL
                        OR gbc.google_location_id IS NOT NULL
                    THEN 1
                    ELSE 0
                END
            ) AS google_is_connected
        FROM businesses b
        LEFT JOIN reviews r
            ON r.business_id = b.id
        LEFT JOIN reports rp
            ON rp.business_id = b.id
        LEFT JOIN google_business_connections gbc
            ON gbc.business_id = b.id
        WHERE b.user_id=%s
        GROUP BY
            b.id,
            b.business_name,
            b.business_type,
            b.city,
            b.state,
            b.country
        ORDER BY b.created_at DESC
        """,
        (user_id,)
    )
    businesses = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        "admin_user_detail.html",
        user=user,
        subscription=subscription,
        latest_payment=latest_payment,
        payments=payments,
        businesses=businesses
    )


@admin_bp.route("/admin/users/<int:user_id>/delete", methods=["POST"])
def admin_delete_user(user_id):
    guard = _admin_required()
    if guard:
        return guard

    if user_id == session.get("user_id"):
        flash("You cannot delete your own admin account while logged in.", "warning")
        return redirect("/admin/users")

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT id, name, email
            FROM users
            WHERE id=%s
            """,
            (user_id,)
        )
        user = cursor.fetchone()

        if not user:
            flash("User was not found.", "danger")
            return redirect("/admin/users")

        cursor.execute(
            """
            SELECT id
            FROM businesses
            WHERE user_id=%s
            """,
            (user_id,)
        )
        business_ids = [row["id"] for row in cursor.fetchall()]

        if business_ids:
            placeholders = ",".join(["%s"] * len(business_ids))
            cursor.execute(
                f"DELETE FROM google_business_performance WHERE business_id IN ({placeholders})",
                tuple(business_ids)
            )
            cursor.execute(
                f"DELETE FROM google_business_connections WHERE business_id IN ({placeholders})",
                tuple(business_ids)
            )
            cursor.execute(
                f"DELETE FROM reports WHERE business_id IN ({placeholders})",
                tuple(business_ids)
            )
            cursor.execute(
                f"DELETE FROM reviews WHERE business_id IN ({placeholders})",
                tuple(business_ids)
            )

        cursor.execute(
            """
            DELETE FROM payments
            WHERE user_id=%s
            """,
            (user_id,)
        )
        cursor.execute(
            """
            DELETE FROM subscriptions
            WHERE user_id=%s
            """,
            (user_id,)
        )
        cursor.execute(
            """
            DELETE FROM businesses
            WHERE user_id=%s
            """,
            (user_id,)
        )
        cursor.execute(
            """
            DELETE FROM users
            WHERE id=%s
            """,
            (user_id,)
        )

        conn.commit()
        flash(f"Deleted {user['name']} and all related records.", "success")
    except Exception:
        conn.rollback()
        flash("User deletion failed. Please try again.", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect("/admin/users")


@admin_bp.route("/admin/payments")
def admin_payments():
    guard = _admin_required()
    if guard:
        return guard

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT
            p.id,
            p.user_id,
            p.amount,
            p.currency,
            p.transaction_id,
            p.payment_status,
            p.created_at,
            p.notes,
            u.name AS user_name,
            u.email AS user_email
        FROM payments p
        JOIN users u
            ON u.id = p.user_id
        ORDER BY
            CASE WHEN p.payment_status='pending' THEN 0 ELSE 1 END,
            p.created_at DESC
        """
    )
    payments = cursor.fetchall()
    cursor.close()
    conn.close()

    return render_template("admin_payments.html", payments=payments)


@admin_bp.route("/admin/payments/<int:payment_id>/approve", methods=["POST"])
def admin_approve_payment(payment_id):
    guard = _admin_required()
    if guard:
        return guard

    if approve_payment(payment_id):
        flash("Payment approved and subscription activated.", "success")
    else:
        flash("Payment was not found.", "danger")

    return redirect("/admin/payments")


@admin_bp.route("/admin/payments/<int:payment_id>/reject", methods=["POST"])
def admin_reject_payment(payment_id):
    guard = _admin_required()
    if guard:
        return guard

    if reject_payment(payment_id):
        flash("Payment rejected.", "warning")
    else:
        flash("Payment was not found.", "danger")

    return redirect("/admin/payments")
