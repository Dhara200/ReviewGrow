import hmac
import secrets
from urllib.parse import urlsplit

from flask import jsonify, request, session
from markupsafe import Markup, escape


CSRF_SESSION_KEY = "_csrf_token"
CSRF_FIELD_NAME = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def init_csrf(app):
    app.before_request(_protect_authenticated_mutation)
    app.context_processor(lambda: {
        "csrf_token": get_csrf_token,
        "csrf_field": csrf_field,
    })


def get_csrf_token():
    if "user_id" not in session:
        return ""
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def csrf_field():
    token = get_csrf_token()
    if not token:
        return Markup("")
    return Markup(
        f'<input type="hidden" name="{CSRF_FIELD_NAME}" '
        f'value="{escape(token)}">'
    )


def _protect_authenticated_mutation():
    if request.method not in UNSAFE_METHODS or "user_id" not in session:
        return None

    expected = session.get(CSRF_SESSION_KEY)
    supplied = request.headers.get(CSRF_HEADER_NAME) or request.form.get(CSRF_FIELD_NAME)
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        return _csrf_failure_response()

    if _is_json_mutation() and not _same_origin_when_supplied():
        return _csrf_failure_response()
    return None


def _same_origin_when_supplied():
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if not source:
        return True
    try:
        return urlsplit(source).netloc.casefold() == request.host.casefold()
    except ValueError:
        return False


def _csrf_failure_response():
    if _is_json_mutation():
        return jsonify({
            "success": False,
            "message": "CSRF validation failed. Refresh the page and try again.",
        }), 403
    return "CSRF validation failed. Refresh the page and try again.", 403


def _is_json_mutation():
    return request.is_json or request.accept_mimetypes.best == "application/json"
