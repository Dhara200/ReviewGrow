from flask import Blueprint, current_app, jsonify, render_template, request, session

from app.services.database_service import get_connection
from app.services.razorpay_service import (
    PaymentError, create_order, handle_webhook, resolve_plan, verify_checkout,
)
from app.services.subscription_service import has_active_subscription, latest_subscription


subscription_bp = Blueprint("subscription", __name__)


def _json_error(error):
    if isinstance(error, PaymentError):
        return jsonify({"success": False, "message": str(error)}), error.status_code
    current_app.logger.exception("Razorpay request failed")
    return jsonify({"success": False, "message": "Payment processing is temporarily unavailable."}), 500


@subscription_bp.route("/pricing")
def pricing_page():
    subscription = None
    is_subscribed = False
    customer = {}
    if "user_id" in session:
        subscription = latest_subscription(session["user_id"])
        is_subscribed = has_active_subscription(session["user_id"])
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT name, email FROM users WHERE id=%s", (session["user_id"],))
        customer = cursor.fetchone() or {}
        cursor.close()
        conn.close()
    plan = resolve_plan("starter_monthly")
    return render_template(
        "pricing.html", subscription_price=plan.amount_paise / 100,
        original_subscription_price=current_app.config["ORIGINAL_SUBSCRIPTION_PRICE"],
        subscription=subscription, is_subscribed=is_subscribed,
        plan_code=plan.code, plan_name=plan.name, customer=customer,
    )


@subscription_bp.post("/payments/razorpay/create-order")
def razorpay_create_order():
    if "user_id" not in session:
        return jsonify({"success": False, "message": "Please login to continue."}), 401
    payload = request.get_json(silent=True) or {}
    if set(payload) - {"plan_code"}:
        return jsonify({"success": False, "message": "Only a plan identifier is accepted."}), 400
    try:
        plan, order_id = create_order(session["user_id"], payload.get("plan_code"))
        return jsonify({
            "success": True, "key_id": current_app.config["RAZORPAY_KEY_ID"],
            "order_id": order_id, "amount": plan.amount_paise,
            "currency": plan.currency, "name": "ReviewGrow",
            "description": plan.name,
        })
    except Exception as exc:
        return _json_error(exc)


@subscription_bp.post("/payments/razorpay/verify")
def razorpay_verify():
    if "user_id" not in session:
        return jsonify({"success": False, "message": "Please login to continue."}), 401
    try:
        _, duplicate = verify_checkout(session["user_id"], request.get_json(silent=True) or {})
        return jsonify({
            "success": True,
            "message": "Payment verified and subscription activated.",
            "already_processed": duplicate,
        })
    except Exception as exc:
        return _json_error(exc)


@subscription_bp.post("/webhooks/razorpay")
def razorpay_webhook():
    raw_body = request.get_data(cache=True)
    try:
        result = handle_webhook(raw_body, request.headers.get("X-Razorpay-Signature"))
        return jsonify({"success": True, "result": result})
    except Exception as exc:
        return _json_error(exc)
