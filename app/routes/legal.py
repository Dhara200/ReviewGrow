from flask import Blueprint, render_template


legal_bp = Blueprint("legal", __name__)

LEGAL_DETAILS = {
    "brand_name": "ReviewGrow",
    "website": "https://reviewgrow.in",
    "owner_name": "Dhara Prasath",
    "support_email": "dharaprasath52@gmail.com",
    "business_address": "15/308, SAYAPATTARAI, AMARAVATHY RF, UDUMALPET TK, TIRUPPUR DIST, TAMIL NADU, 642102",
    "jurisdiction": "Coimbatore, Tamil Nadu, India",
    "refund_period": "On Subscription end date",
}


def _render_legal_page(template_name):
    return render_template(template_name, legal=LEGAL_DETAILS)


@legal_bp.route("/privacy-policy")
def privacy_policy():
    return _render_legal_page("privacy_policy.html")


@legal_bp.route("/terms-of-service")
def terms_of_service():
    return _render_legal_page("terms_of_service.html")


@legal_bp.route("/data-deletion")
def data_deletion():
    return _render_legal_page("data_deletion.html")
