def init_session_security(app):
    validate_session_security(app)

    @app.after_request
    def prevent_authenticated_response_caching(response):
        from flask import request, session

        if "user_id" in session and request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        return response


def validate_session_security(app):
    if app.config.get("APP_ENV") != "production":
        return

    secret_key = app.config.get("SECRET_KEY") or ""
    if len(secret_key) < 32:
        raise RuntimeError(
            "Production SECRET_KEY must be configured with at least 32 characters."
        )
    if app.config.get("SESSION_COOKIE_SECURE") is not True:
        raise RuntimeError("Production session cookies must use Secure.")
