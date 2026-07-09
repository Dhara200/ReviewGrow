import os

from flask import Blueprint, current_app, flash, redirect, render_template, request, session

from app.config import Config
from app.services.subscription_service import (
    has_active_subscription,
    latest_subscription,
    pending_payment,
    submit_manual_upi_payment
)


subscription_bp = Blueprint("subscription", __name__)


@subscription_bp.route("/pricing")
def pricing_page():
    subscription = None
    payment = None
    is_subscribed = False

    if "user_id" in session:
        subscription = latest_subscription(session["user_id"])
        payment = pending_payment(session["user_id"])
        is_subscribed = has_active_subscription(session["user_id"])

    return render_template(
        "pricing.html",
        upi_id=Config.UPI_ID,
        subscription_price=Config.SUBSCRIPTION_PRICE,
        original_subscription_price=Config.ORIGINAL_SUBSCRIPTION_PRICE,
        upi_qr_exists=os.path.exists(
            os.path.join(current_app.static_folder, "images", "upi_qr.png")
        ),
        subscription=subscription,
        pending_payment=payment,
        is_subscribed=is_subscribed
    )


@subscription_bp.route("/pricing/submit-payment", methods=["POST"])
def submit_payment_reference():
    if "user_id" not in session:
        flash("Please login before submitting payment details.", "warning")
        return redirect("/login-page")

    transaction_id = (request.form.get("transaction_id") or "").strip()
    notes = (request.form.get("notes") or "").strip()

    if not transaction_id:
        flash("Please enter your UPI transaction or reference ID.", "danger")
        return redirect("/pricing")

    payment, created = submit_manual_upi_payment(
        session["user_id"],
        Config.SUBSCRIPTION_PRICE,
        transaction_id,
        notes
    )

    if created:
        flash("Payment submitted. Admin will verify and activate your subscription.", "success")
    else:
        flash("You already have a pending payment. Admin will verify it soon.", "info")

    return redirect("/pricing")
