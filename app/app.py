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

app = Flask(__name__)
app.secret_key = Config.SECRET_KEY

app.register_blueprint(auth_bp)
app.register_blueprint(business_bp)
app.register_blueprint(review_bp)
app.register_blueprint(analysis_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(google_business_bp)
app.register_blueprint(subscription_bp)

try:
    ensure_mvp_schema()
except Exception as e:
    print(f"Schema check skipped: {e}")

@app.route("/")
def home():
    if "user_id" in session:

        return redirect(
            "/my-businesses"
        )

    return render_template(
        "index.html"
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
