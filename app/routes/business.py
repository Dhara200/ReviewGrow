from flask import (
    Blueprint,
    request,
    jsonify,
    session,
    render_template,
    redirect
)
from app.services.database_service import (
    get_connection,
    user_owns_business
)
from app.services.subscription_service import subscription_required

business_bp = Blueprint("businesses", __name__)

#CREATE BUSINESS PAGE ROUTE

@business_bp.route("/create-business")
@subscription_required
def create_business_page():

    if "user_id" not in session:

        return redirect("/login-page")

    return render_template(
        "create_business.html"
    )
    
@business_bp.route(
    "/create-business-ui",
    methods=["POST"]
)
@subscription_required

#CREATE BUSINESS FORM SUBMISSION ROUTE

def create_business_ui():

    try:

        if "user_id" not in session:
            return redirect("/login-page")

        business_name = request.form.get(
            "business_name"
        )

        business_type = request.form.get(
            "business_type"
        )

        city = request.form.get("city")
        state = request.form.get("state")
        country = request.form.get("country")

        conn = get_connection()

        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO businesses
            (
                user_id,
                business_name,
                business_type,
                city,
                state,
                country
            )
            VALUES
            (%s,%s,%s,%s,%s,%s)
            """,
            (
                session["user_id"],
                business_name,
                business_type,
                city,
                state,
                country
            )
        )

        conn.commit()

        cursor.close()
        conn.close()

        return redirect(
            "/my-businesses"
        )

    except Exception as e:

        return jsonify({
            "message": str(e)
        }), 500

# MY BUSINESSES PAGE ROUTE 

@business_bp.route("/my-businesses", methods=["GET"])
@subscription_required
def my_businesses():
    try:
        if "user_id" not in session:
            return redirect("/login-page")

        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT
                b.*,
                gbc.is_connected AS google_is_connected,
                gbc.google_location_name
            FROM businesses b
            LEFT JOIN google_business_connections gbc
                ON gbc.business_id = b.id
                AND gbc.user_id = b.user_id
                AND gbc.is_connected = TRUE
            WHERE b.user_id=%s
            ORDER BY b.id DESC
            """,
            (session["user_id"],)
        )

        businesses = cursor.fetchall()
        cursor.close()
        conn.close()
        return render_template(
            "my_businesses.html",
            businesses=businesses,
            user_name=session["user_name"]
        )
    except Exception as e:

        return jsonify({
            "message": str(e)
        }), 500


@business_bp.route("/business/delete/<int:business_id>", methods=["POST"])
@subscription_required
def delete_business(business_id):

    if "user_id" not in session:

        return redirect("/login-page")

    try:

        if not user_owns_business(
            session["user_id"],
            business_id
        ):

            return "Access denied", 403

        conn = get_connection()

        cursor = conn.cursor()

        cursor.execute(
            """
            DELETE FROM businesses
            WHERE id=%s
            AND user_id=%s
            """,
            (
                business_id,
                session["user_id"]
            )
        )

        conn.commit()

        cursor.close()
        conn.close()

        return redirect("/my-businesses")

    except Exception as e:

        return jsonify({
            "message": str(e)
        }), 500
        
 #UPLOAD REVIEWS PAGE ROUTE
        
@business_bp.route("/upload-reviews/<int:business_id>")
@subscription_required
def upload_reviews_page(business_id):

    try:

        if "user_id" not in session:

            return redirect("/login-page")

        if not user_owns_business(
            session["user_id"],
            business_id
        ):

            return "Access denied", 403

        conn = get_connection()

        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            """
            SELECT
                id,
                business_name,
                business_type
            FROM businesses
            WHERE id=%s
            """,
            (business_id,)
        )

        business = cursor.fetchone()

        cursor.close()
        conn.close()

        return render_template(
            "upload_reviews.html",
            business=business,
            business_id=business_id
        )

    except Exception as e:

        return jsonify({
            "message": str(e)
        }), 500
   
