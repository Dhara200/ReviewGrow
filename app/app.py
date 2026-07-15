from pathlib import Path

from flask import Flask
from app.services.database_service import get_connection, ensure_mvp_schema
from app.routes.auth import auth_bp
from app.routes.business import business_bp
from app.routes.reviews import review_bp
from app.routes.analysis import analysis_bp
from app.routes.dashboard import dashboard_bp
from app.config import Config
from flask import render_template
from flask import session
from flask import redirect
from app.routes.admin import admin_bp
from app.routes.google_business import google_business_bp
from app.routes.subscription import subscription_bp
from app.routes.ai_consultant_routes import ai_consultant_bp
from app.routes.legal import legal_bp

app = Flask(__name__)
app.config.from_object(Config)
app.secret_key = Config.SECRET_KEY


@app.context_processor
def recaptcha_template_config():
    return {
        "recaptcha_enabled": app.config["RECAPTCHA_ENABLED"],
        "recaptcha_site_key": app.config["RECAPTCHA_SITE_KEY"]
    }

app.register_blueprint(auth_bp)
app.register_blueprint(business_bp)
app.register_blueprint(review_bp)
app.register_blueprint(analysis_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(google_business_bp)
app.register_blueprint(subscription_bp)
app.register_blueprint(ai_consultant_bp)
app.register_blueprint(legal_bp)

try:
    ensure_mvp_schema()
except Exception as e:
    print(f"Schema check skipped: {e}")


def get_landing_hero_images():
    allowed_extensions = {".jpg", ".jpeg", ".png", ".webp", ".avif"}
    image_root = Path(app.root_path) / "static" / "images"
    candidates = []

    for folder in (image_root / "landing", image_root):
        if not folder.exists():
            continue

        for image_path in sorted(folder.iterdir()):
            if not image_path.is_file():
                continue
            if image_path.suffix.lower() not in allowed_extensions:
                continue
            if image_path.name.lower().startswith("logo"):
                continue

            candidates.append(image_path.relative_to(image_root.parent).as_posix())

        if candidates:
            break

    return candidates[:4]

@app.route("/")
def home():
    if "user_id" in session:

        return redirect(
            "/my-businesses"
        )

    return render_template(
        "index.html",
        landing_hero_images=get_landing_hero_images(),
        subscription_price=Config.SUBSCRIPTION_PRICE,
        original_subscription_price=Config.ORIGINAL_SUBSCRIPTION_PRICE
    )


@app.route("/health")
def health():

    try:
        conn = get_connection()

        cursor = conn.cursor()

        cursor.execute("SELECT 1")

        result = cursor.fetchone()

        cursor.close()
        conn.close()

        return {
            "status": "healthy",
            "database": "connected",
            "result": result[0]
        }

    except Exception as e:

        return {
            "status": "error",
            "message": str(e)
        }, 500



if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
