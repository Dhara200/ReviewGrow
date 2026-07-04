from app.services.database_service import get_connection


def refresh_ai_monthly_usage():
    conn = get_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            INSERT INTO ai_monthly_usage
            (
                user_id,
                business_id,
                provider,
                model_name,
                usage_month,
                total_requests,
                successful_requests,
                failed_requests,
                total_input_tokens,
                total_output_tokens,
                total_tokens,
                total_estimated_cost,
                average_response_time_ms
            )
            SELECT
                user_id,
                business_id,
                provider,
                model_name,
                DATE_FORMAT(created_at, '%Y-%m-01') AS usage_month,
                COUNT(*) AS total_requests,
                SUM(CASE WHEN request_status='success' THEN 1 ELSE 0 END) AS successful_requests,
                SUM(CASE WHEN request_status='failed' THEN 1 ELSE 0 END) AS failed_requests,
                COALESCE(SUM(input_tokens), 0) AS total_input_tokens,
                COALESCE(SUM(output_tokens), 0) AS total_output_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(estimated_cost), 0) AS total_estimated_cost,
                COALESCE(AVG(response_time_ms), 0) AS average_response_time_ms
            FROM ai_usage_logs
            WHERE business_id IS NOT NULL
            GROUP BY
                user_id,
                business_id,
                provider,
                model_name,
                usage_month
            ON DUPLICATE KEY UPDATE
                total_requests=VALUES(total_requests),
                successful_requests=VALUES(successful_requests),
                failed_requests=VALUES(failed_requests),
                total_input_tokens=VALUES(total_input_tokens),
                total_output_tokens=VALUES(total_output_tokens),
                total_tokens=VALUES(total_tokens),
                total_estimated_cost=VALUES(total_estimated_cost),
                average_response_time_ms=VALUES(average_response_time_ms)
            """
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()
