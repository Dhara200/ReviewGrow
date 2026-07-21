import json
import logging
from dataclasses import dataclass

import razorpay

from app.config import Config
from app.services.database_service import get_connection
from app.services.subscription_service import activate_or_extend_subscription


logger = logging.getLogger(__name__)


class PaymentError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class Plan:
    code: str
    name: str
    amount_paise: int
    currency: str = "INR"
    duration_days: int = 30


def resolve_plan(plan_code):
    plans = {
        "starter_monthly": Plan(
            "starter_monthly", "ReviewGrow Premium",
            int(round(Config.SUBSCRIPTION_PRICE * 100))
        )
    }
    plan = plans.get((plan_code or "").strip())
    if not plan or plan.amount_paise <= 0:
        raise PaymentError("Unknown or inactive plan.")
    return plan


def get_razorpay_client():
    if not Config.RAZORPAY_KEY_ID or not Config.RAZORPAY_KEY_SECRET:
        raise PaymentError("Payment service is not configured.", 503)
    return razorpay.Client(auth=(Config.RAZORPAY_KEY_ID, Config.RAZORPAY_KEY_SECRET))


def create_order(user_id, plan_code, client=None):
    plan = resolve_plan(plan_code)
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            INSERT INTO payments
            (user_id, amount, amount_paise, currency, payment_method,
             payment_status, transaction_id, payment_gateway, plan_code)
            VALUES (%s,%s,%s,%s,'razorpay','created',%s,'razorpay',%s)
            """,
            (user_id, plan.amount_paise / 100, plan.amount_paise, plan.currency,
             "local-pending", plan.code)
        )
        local_id = cursor.lastrowid
        provider = client or get_razorpay_client()
        order = provider.order.create({
            "amount": plan.amount_paise,
            "currency": plan.currency,
            "receipt": f"rg-{local_id}",
            "notes": {"payment_id": str(local_id), "user_id": str(user_id), "plan": plan.code},
        })
        order_id = order.get("id")
        if not order_id or int(order.get("amount", -1)) != plan.amount_paise or order.get("currency") != plan.currency:
            raise PaymentError("Payment provider returned an invalid order.", 502)
        cursor.execute(
            """UPDATE payments SET razorpay_order_id=%s, transaction_id=%s,
               payment_status='attempted' WHERE id=%s""",
            (order_id, order_id, local_id)
        )
        conn.commit()
        logger.info("razorpay_order_created local_payment_id=%s order_id=%s", local_id, order_id)
        return plan, order_id
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def _provider_is_paid(client, order_id, payment_id, amount_paise, currency):
    payment = client.payment.fetch(payment_id)
    if payment.get("order_id") != order_id:
        raise PaymentError("Payment does not match the order.")
    if int(payment.get("amount", -1)) != amount_paise or payment.get("currency") != currency:
        raise PaymentError("Payment amount or currency mismatch.")
    if payment.get("status") == "captured":
        return True
    order = client.order.fetch(order_id)
    return order.get("status") == "paid" and int(order.get("amount_paid", 0)) >= amount_paise


def process_success(order_id, payment_id, user_id=None, signature=None, client=None):
    provider = client or get_razorpay_client()
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM payments WHERE razorpay_order_id=%s FOR UPDATE", (order_id,))
        payment = cursor.fetchone()
        if not payment or payment.get("payment_gateway") != "razorpay":
            raise PaymentError("Unknown payment order.", 404)
        if user_id is not None and payment["user_id"] != user_id:
            raise PaymentError("This payment order belongs to another account.", 403)
        if payment.get("processed_at") is not None:
            conn.commit()
            return True, True
        plan = resolve_plan(payment.get("plan_code"))
        if payment.get("amount_paise") != plan.amount_paise or payment.get("currency") != plan.currency:
            raise PaymentError("Stored payment amount or currency mismatch.")
        if signature is not None:
            try:
                provider.utility.verify_payment_signature({
                    "razorpay_order_id": order_id,
                    "razorpay_payment_id": payment_id,
                    "razorpay_signature": signature,
                })
            except Exception as exc:
                raise PaymentError("Payment signature verification failed.", 400) from exc
        if not _provider_is_paid(provider, order_id, payment_id, plan.amount_paise, plan.currency):
            raise PaymentError("Payment has not been captured.", 409)
        subscription_id, _ = activate_or_extend_subscription(
            cursor, payment["user_id"], "starter", plan.duration_days
        )
        cursor.execute(
            """
            UPDATE payments SET razorpay_payment_id=%s, transaction_id=%s,
                payment_status='paid', paid_at=NOW(), processed_at=NOW(),
                subscription_id=%s, failure_code=NULL, failure_reason=NULL
            WHERE id=%s AND processed_at IS NULL
            """,
            (payment_id, payment_id, subscription_id, payment["id"])
        )
        conn.commit()
        logger.info("razorpay_payment_processed local_payment_id=%s order_id=%s payment_id=%s duplicate=false", payment["id"], order_id, payment_id)
        return True, False
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def verify_checkout(user_id, payload, client=None):
    required = ("razorpay_order_id", "razorpay_payment_id", "razorpay_signature")
    if not all(payload.get(field) for field in required):
        raise PaymentError("Required payment verification fields are missing.")
    return process_success(
        payload["razorpay_order_id"], payload["razorpay_payment_id"], user_id,
        payload["razorpay_signature"], client
    )


def process_failed(order_id, payment_id=None, code=None, reason=None):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM payments WHERE razorpay_order_id=%s FOR UPDATE", (order_id,))
        payment = cursor.fetchone()
        if payment and payment.get("payment_gateway") == "razorpay" and not payment.get("processed_at"):
            cursor.execute(
                """UPDATE payments SET payment_status='failed', razorpay_payment_id=COALESCE(%s, razorpay_payment_id),
                   failure_code=%s, failure_reason=%s WHERE id=%s""",
                (payment_id, (code or "")[:100] or None, (reason or "")[:255] or None, payment["id"])
            )
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def handle_webhook(raw_body, signature, client=None):
    if not Config.RAZORPAY_WEBHOOK_SECRET:
        raise PaymentError("Webhook processing is not configured.", 503)
    if not signature:
        raise PaymentError("Missing webhook signature.", 401)
    provider = client or get_razorpay_client()
    try:
        provider.utility.verify_webhook_signature(
            raw_body.decode("utf-8"), signature, Config.RAZORPAY_WEBHOOK_SECRET
        )
    except Exception as exc:
        raise PaymentError("Invalid webhook signature.", 401) from exc
    try:
        event = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PaymentError("Invalid webhook body.") from exc
    event_type = event.get("event")
    entities = event.get("payload") or {}
    payment = ((entities.get("payment") or {}).get("entity") or {})
    order = ((entities.get("order") or {}).get("entity") or {})
    order_id = payment.get("order_id") or order.get("id")
    payment_id = payment.get("id") or order.get("payment_id")
    if event_type in {"order.paid", "payment.captured"} and order_id and payment_id:
        _, duplicate = process_success(order_id, payment_id, client=provider)
        result = "duplicate" if duplicate else "processed"
    elif event_type == "payment.failed" and order_id:
        error = payment.get("error_description") or "Payment failed"
        process_failed(order_id, payment_id, payment.get("error_code"), error)
        result = "failed_recorded"
    else:
        result = "ignored"
    logger.info("razorpay_webhook event=%s order_id=%s payment_id=%s result=%s", event_type, order_id, payment_id, result)
    return result
