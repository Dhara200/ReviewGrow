from app.services.analysis_job_service import create_analysis_job, process_analysis_job
from app.services.database_service import get_connection


def process_business_reviews(business_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT user_id
        FROM businesses
        WHERE id=%s
        """,
        (business_id,)
    )
    business = cursor.fetchone()
    cursor.close()
    conn.close()

    if not business:
        return

    job_id, created = create_analysis_job(
        user_id=business["user_id"],
        business_id=business_id,
        force_reanalysis=False
    )
    if created:
        process_analysis_job(job_id)
