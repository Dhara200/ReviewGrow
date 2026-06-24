from flask import Flask
from app.services.database_service import get_connection

app = Flask(__name__)


@app.route("/")
def home():
    return "AI Reputation Manager is running"


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
    app.run(host="0.0.0.0", port=5000)