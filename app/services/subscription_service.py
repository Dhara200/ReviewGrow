from datetime import datetime, timedelta
from functools import wraps

from flask import flash, redirect, session

from app.services.database_service import get_connection


def latest_subscription(user_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT *
        FROM subscriptions
        WHERE user_id=%s
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (user_id,)
    )
    subscription = cursor.fetchone()
    cursor.close()
    conn.close()

    return subscription


def has_active_subscription(user_id):
    if session.get("role") == "admin":
        return True

    subscription = latest_subscription(user_id)

    if not subscription:
        return False

    end_date = subscription.get("subscription_end_date")

    return (
        subscription.get("status") == "active"
        and end_date is not None
        and end_date > datetime.utcnow()
    )


def subscription_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect("/login-page")

        if session.get("role") == "admin" or has_active_subscription(session["user_id"]):
            return view_func(*args, **kwargs)

        flash("Please subscribe to access ReviewGrow.", "warning")
        return redirect("/pricing")

    return wrapper


def create_expired_subscription(user_id, connection=None, cursor=None):
    owns_connection = connection is None
    conn = connection or get_connection()
    active_cursor = cursor or conn.cursor()
    try:
        active_cursor.execute(
            """
            INSERT INTO subscriptions
            (
                user_id,
                plan_name,
                status,
                subscription_start_date,
                subscription_end_date,
                review_credits
            )
            VALUES
            (%s,%s,%s,%s,%s,%s)
            """,
            (
                user_id,
                "starter",
                "expired",
                None,
                None,
                0
            )
        )
        if owns_connection:
            conn.commit()
    except Exception:
        if owns_connection:
            conn.rollback()
        raise
    finally:
        if cursor is None:
            active_cursor.close()
        if owns_connection:
            conn.close()


def approve_payment(payment_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT *
        FROM payments
        WHERE id=%s
        """,
        (payment_id,)
    )
    payment = cursor.fetchone()

    if not payment:
        cursor.close()
        conn.close()
        return False

    start_date = datetime.utcnow()
    end_date = start_date + timedelta(days=30)

    cursor.execute(
        """
        UPDATE payments
        SET payment_status='success',
            paid_at=NOW()
        WHERE id=%s
        """,
        (payment_id,)
    )

    subscription_id = payment.get("subscription_id")

    if subscription_id:
        cursor.execute(
            """
            UPDATE subscriptions
            SET plan_name='starter',
                status='active',
                subscription_start_date=%s,
                subscription_end_date=%s,
                review_credits=500
            WHERE id=%s
            """,
            (start_date, end_date, subscription_id)
        )
    else:
        cursor.execute(
            """
            INSERT INTO subscriptions
            (
                user_id,
                plan_name,
                status,
                subscription_start_date,
                subscription_end_date,
                review_credits
            )
            VALUES
            (%s,%s,%s,%s,%s,%s)
            """,
            (
                payment["user_id"],
                "starter",
                "active",
                start_date,
                end_date,
                500
            )
        )

    conn.commit()
    cursor.close()
    conn.close()

    return True


def activate_or_extend_subscription(cursor, user_id, plan_name="starter", duration_days=30):
    """Activate/extend a user's subscription using the caller's transaction."""
    cursor.execute(
        """
        SELECT * FROM subscriptions
        WHERE user_id=%s
        ORDER BY created_at DESC, id DESC
        LIMIT 1 FOR UPDATE
        """,
        (user_id,)
    )
    subscription = cursor.fetchone()
    now = datetime.utcnow()
    current_end = subscription.get("subscription_end_date") if subscription else None
    start_from = current_end if current_end and current_end > now else now
    new_end = start_from + timedelta(days=duration_days)

    if subscription:
        cursor.execute(
            """
            UPDATE subscriptions
            SET plan_name=%s, status='active',
                subscription_start_date=COALESCE(subscription_start_date, %s),
                subscription_end_date=%s, review_credits=500
            WHERE id=%s
            """,
            (plan_name, now, new_end, subscription["id"])
        )
        return subscription["id"], new_end

    cursor.execute(
        """
        INSERT INTO subscriptions
        (user_id, plan_name, status, subscription_start_date,
         subscription_end_date, review_credits)
        VALUES (%s,%s,'active',%s,%s,500)
        """,
        (user_id, plan_name, now, new_end)
    )
    return cursor.lastrowid, new_end


def reject_payment(payment_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE payments
        SET payment_status='rejected'
        WHERE id=%s
        """,
        (payment_id,)
    )
    changed = cursor.rowcount > 0
    conn.commit()
    cursor.close()
    conn.close()

    return changed
