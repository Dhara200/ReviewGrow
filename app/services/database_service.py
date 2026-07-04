import mysql.connector
from flask import session

from app.config import Config


def get_connection():
    return mysql.connector.connect(
        host=Config.DB_HOST,
        port=Config.DB_PORT,
        user=Config.DB_USER,
        password=Config.DB_PASSWORD,
        database=Config.DB_NAME
    )


def _column_exists(cursor, table_name, column_name):
    cursor.execute(
        """
        SELECT COUNT(*) AS column_count
        FROM information_schema.columns
        WHERE table_schema=%s
        AND table_name=%s
        AND column_name=%s
        """,
        (Config.DB_NAME, table_name, column_name)
    )

    result = cursor.fetchone()

    if isinstance(result, dict):
        return result["column_count"] > 0

    return result[0] > 0


def _add_column_if_missing(cursor, table_name, column_name, definition):
    if _column_exists(cursor, table_name, column_name):
        return

    cursor.execute(
        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
    )


def _table_exists(cursor, table_name):
    cursor.execute(
        """
        SELECT COUNT(*) AS table_count
        FROM information_schema.tables
        WHERE table_schema=%s
        AND table_name=%s
        """,
        (Config.DB_NAME, table_name)
    )

    result = cursor.fetchone()

    if isinstance(result, dict):
        return result["table_count"] > 0

    return result[0] > 0


def _create_table_if_missing(cursor, table_name, create_sql):
    if _table_exists(cursor, table_name):
        return

    cursor.execute(create_sql)


def _index_exists(cursor, table_name, index_name):
    cursor.execute(
        """
        SELECT COUNT(*) AS index_count
        FROM information_schema.statistics
        WHERE table_schema=%s
        AND table_name=%s
        AND index_name=%s
        """,
        (Config.DB_NAME, table_name, index_name)
    )

    result = cursor.fetchone()

    if isinstance(result, dict):
        return result["index_count"] > 0

    return result[0] > 0


def _add_index_if_missing(cursor, table_name, index_name, definition):
    if _index_exists(cursor, table_name, index_name):
        return

    cursor.execute(
        f"ALTER TABLE {table_name} ADD {definition}"
    )


def ensure_mvp_schema():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    _create_table_if_missing(
        cursor,
        "subscriptions",
        """
        CREATE TABLE subscriptions (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            plan_name VARCHAR(50) NOT NULL,
            status ENUM('active','expired','cancelled','disabled') DEFAULT 'expired',
            subscription_start_date DATETIME NULL,
            subscription_end_date DATETIME NULL,
            review_credits INT DEFAULT 500,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_subscriptions_user_id (user_id),
            INDEX idx_subscriptions_status (status),
            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE
        )
        """
    )
    _create_table_if_missing(
        cursor,
        "payments",
        """
        CREATE TABLE payments (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            subscription_id INT NULL,
            amount DECIMAL(10,2) NOT NULL,
            currency VARCHAR(10) DEFAULT 'INR',
            payment_method VARCHAR(50) DEFAULT 'UPI',
            payment_status ENUM('pending','success','failed','rejected') DEFAULT 'pending',
            transaction_id VARCHAR(255) NOT NULL,
            payment_gateway VARCHAR(50) DEFAULT 'manual_upi',
            paid_at DATETIME NULL,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_payments_user_id (user_id),
            INDEX idx_payments_subscription_id (subscription_id),
            INDEX idx_payments_payment_status (payment_status),
            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE,
            FOREIGN KEY (subscription_id)
                REFERENCES subscriptions(id)
                ON DELETE SET NULL
        )
        """
    )
    _create_table_if_missing(
        cursor,
        "login_attempts",
        """
        CREATE TABLE login_attempts (
            id INT AUTO_INCREMENT PRIMARY KEY,
            email VARCHAR(255),
            ip_address VARCHAR(100),
            failed_attempts INT DEFAULT 0,
            locked_until DATETIME NULL,
            last_failed_at DATETIME,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uniq_login_attempt_email_ip (email, ip_address)
        )
        """
    )

    _add_column_if_missing(
        cursor,
        "users",
        "role",
        "VARCHAR(50) NOT NULL DEFAULT 'owner'"
    )
    _add_column_if_missing(
        cursor,
        "reviews",
        "sentiment",
        "VARCHAR(50)"
    )
    _add_column_if_missing(
        cursor,
        "reviews",
        "summary",
        "TEXT"
    )
    _add_column_if_missing(
        cursor,
        "reviews",
        "ai_reply",
        "TEXT"
    )
    _add_column_if_missing(
        cursor,
        "reviews",
        "analyzed_at",
        "DATETIME"
    )
    _add_column_if_missing(
        cursor,
        "reviews",
        "category",
        "VARCHAR(100)"
    )
    _add_column_if_missing(
        cursor,
        "reviews",
        "complaint_praise_theme",
        "VARCHAR(255)"
    )
    _add_column_if_missing(
        cursor,
        "reviews",
        "suggested_reply",
        "TEXT"
    )
    _add_column_if_missing(
        cursor,
        "reviews",
        "confidence_score",
        "DECIMAL(5,4)"
    )
    _add_column_if_missing(
        cursor,
        "reviews",
        "analysis_error",
        "TEXT"
    )
    _add_column_if_missing(
        cursor,
        "reports",
        "recommendations",
        "JSON"
    )
    _add_column_if_missing(
        cursor,
        "reviews",
        "external_review_id",
        "VARCHAR(255)"
    )
    _add_column_if_missing(
        cursor,
        "reviews",
        "review_rating",
        "DECIMAL(2,1)"
    )
    _add_column_if_missing(
        cursor,
        "reviews",
        "review_created_at",
        "DATETIME"
    )
    _add_column_if_missing(
        cursor,
        "reviews",
        "review_updated_at",
        "DATETIME"
    )
    _add_column_if_missing(
        cursor,
        "reviews",
        "google_location_id",
        "VARCHAR(255) NULL"
    )
    _add_index_if_missing(
        cursor,
        "reviews",
        "idx_reviews_business_google_location",
        "INDEX idx_reviews_business_google_location (business_id, google_location_id)"
    )
    _create_table_if_missing(
        cursor,
        "google_business_connections",
        """
        CREATE TABLE google_business_connections (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            business_id INT NOT NULL,
            google_account_id VARCHAR(255),
            google_location_id VARCHAR(255),
            google_location_name VARCHAR(255),
            google_account_email VARCHAR(255),
            access_token TEXT,
            refresh_token TEXT,
            token_expiry DATETIME,
            scope TEXT,
            scopes TEXT,
            connection_status VARCHAR(50) DEFAULT 'connected',
            is_connected BOOLEAN DEFAULT FALSE,
            last_sync_at DATETIME,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY unique_business_connection (user_id, business_id),
            UNIQUE KEY uniq_google_business_business (business_id),
            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE,
            FOREIGN KEY (business_id)
                REFERENCES businesses(id)
                ON DELETE CASCADE
        )
        """
    )
    _add_column_if_missing(
        cursor,
        "google_business_connections",
        "google_account_email",
        "VARCHAR(255)"
    )
    _add_column_if_missing(
        cursor,
        "google_business_connections",
        "scopes",
        "TEXT"
    )
    _add_column_if_missing(
        cursor,
        "google_business_connections",
        "connection_status",
        "VARCHAR(50) DEFAULT 'connected'"
    )
    _add_index_if_missing(
        cursor,
        "google_business_connections",
        "unique_business_connection",
        "UNIQUE KEY unique_business_connection (user_id, business_id)"
    )
    _create_table_if_missing(
        cursor,
        "google_business_performance",
        """
        CREATE TABLE google_business_performance (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            business_id INT NOT NULL,
            google_location_id VARCHAR(255) NOT NULL,
            metric_name VARCHAR(120) NOT NULL,
            metric_value BIGINT DEFAULT 0,
            metric_date DATE NOT NULL,
            period_start DATE NOT NULL,
            period_end DATE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_google_perf_business_date (business_id, metric_date),
            UNIQUE KEY uniq_google_perf_metric (
                business_id,
                google_location_id,
                metric_name,
                metric_date,
                period_start,
                period_end
            ),
            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE,
            FOREIGN KEY (business_id)
                REFERENCES businesses(id)
                ON DELETE CASCADE
        )
        """
    )
    _create_table_if_missing(
        cursor,
        "analysis_jobs",
        """
        CREATE TABLE analysis_jobs (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            business_id INT NOT NULL,
            status ENUM('pending','processing','completed','failed') DEFAULT 'pending',
            total_reviews INT DEFAULT 0,
            processed_reviews INT DEFAULT 0,
            failed_reviews INT DEFAULT 0,
            error_message TEXT,
            force_reanalysis BOOLEAN DEFAULT FALSE,
            latest_report_id INT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at DATETIME NULL,
            completed_at DATETIME NULL,
            INDEX idx_analysis_jobs_status (status),
            INDEX idx_analysis_jobs_user_id (user_id),
            INDEX idx_analysis_jobs_business_id (business_id),
            INDEX idx_analysis_jobs_created_at (created_at),
            INDEX idx_analysis_jobs_business_status (business_id, status),
            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE,
            FOREIGN KEY (business_id)
                REFERENCES businesses(id)
                ON DELETE CASCADE
        )
        """
    )
    _create_table_if_missing(
        cursor,
        "ai_usage_logs",
        """
        CREATE TABLE ai_usage_logs (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            business_id INT NULL,
            provider VARCHAR(50) NOT NULL,
            model_name VARCHAR(100) NOT NULL,
            operation_type VARCHAR(100) NOT NULL,
            input_tokens INT DEFAULT 0,
            output_tokens INT DEFAULT 0,
            total_tokens INT DEFAULT 0,
            estimated_cost DECIMAL(12,6) DEFAULT 0,
            request_status ENUM('success','failed') DEFAULT 'success',
            response_time_ms INT DEFAULT 0,
            error_message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_ai_usage_user_id (user_id),
            INDEX idx_ai_usage_business_id (business_id),
            INDEX idx_ai_usage_created_at (created_at),
            INDEX idx_ai_usage_provider_model (provider, model_name),
            INDEX idx_ai_usage_month (created_at, user_id, business_id),
            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE,
            FOREIGN KEY (business_id)
                REFERENCES businesses(id)
                ON DELETE SET NULL
        )
        """
    )
    _create_table_if_missing(
        cursor,
        "ai_monthly_usage",
        """
        CREATE TABLE ai_monthly_usage (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            business_id INT NULL,
            provider VARCHAR(50) NOT NULL,
            model_name VARCHAR(100) NOT NULL,
            usage_month DATE NOT NULL,
            total_requests INT DEFAULT 0,
            successful_requests INT DEFAULT 0,
            failed_requests INT DEFAULT 0,
            total_input_tokens BIGINT DEFAULT 0,
            total_output_tokens BIGINT DEFAULT 0,
            total_tokens BIGINT DEFAULT 0,
            total_estimated_cost DECIMAL(12,6) DEFAULT 0,
            average_response_time_ms DECIMAL(12,2) DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uniq_ai_monthly_usage (
                user_id,
                business_id,
                provider,
                model_name,
                usage_month
            ),
            INDEX idx_ai_monthly_user (user_id),
            INDEX idx_ai_monthly_business (business_id),
            INDEX idx_ai_monthly_month (usage_month),
            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE,
            FOREIGN KEY (business_id)
                REFERENCES businesses(id)
                ON DELETE SET NULL
        )
        """
    )

    conn.commit()
    cursor.close()
    conn.close()
    
def user_owns_business(user_id, business_id):
    if session.get("role") == "admin":
        return True

    conn = get_connection()

    cursor = conn.cursor(dictionary=True)

    cursor.execute(
        """
        SELECT id
        FROM businesses
        WHERE id=%s
        AND user_id=%s
        """,
        (business_id, user_id)
    )

    business = cursor.fetchone()

    cursor.close()
    conn.close()

    return business is not None
