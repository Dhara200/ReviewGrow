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


def _modify_column_if_needed(cursor, table_name, column_name, definition):
    cursor.execute(
        """
        SELECT column_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema=%s
        AND table_name=%s
        AND column_name=%s
        """,
        (Config.DB_NAME, table_name, column_name)
    )
    column = cursor.fetchone()
    if not column:
        return

    cursor.execute(
        f"ALTER TABLE {table_name} MODIFY COLUMN {column_name} {definition}"
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
            payment_status ENUM('pending','success','failed','rejected','created','attempted','paid','refunded','needs_review') DEFAULT 'pending',
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
    _add_column_if_missing(cursor, "payments", "plan_code", "VARCHAR(50) NULL")
    _add_column_if_missing(cursor, "payments", "amount_paise", "BIGINT UNSIGNED NULL")
    _add_column_if_missing(cursor, "payments", "razorpay_order_id", "VARCHAR(255) NULL")
    _add_column_if_missing(cursor, "payments", "razorpay_payment_id", "VARCHAR(255) NULL")
    _add_column_if_missing(cursor, "payments", "failure_code", "VARCHAR(100) NULL")
    _add_column_if_missing(cursor, "payments", "failure_reason", "VARCHAR(255) NULL")
    _add_column_if_missing(cursor, "payments", "processed_at", "DATETIME NULL")
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
        "source",
        "VARCHAR(100) NOT NULL DEFAULT 'excel'"
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
        "google_review_id",
        "VARCHAR(255)"
    )
    _add_column_if_missing(
        cursor,
        "reviews",
        "source_platform",
        "VARCHAR(50) NOT NULL DEFAULT 'google'"
    )
    cursor.execute(
        """
        UPDATE reviews
        SET source_platform=source
        WHERE source_platform='google'
        AND source IS NOT NULL
        AND source <> ''
        """
    )
    _add_column_if_missing(
        cursor,
        "reviews",
        "reply_status",
        "ENUM('pending','approved','posted','failed') DEFAULT 'pending'"
    )
    _add_column_if_missing(
        cursor,
        "reviews",
        "reply_generated_at",
        "DATETIME"
    )
    _add_column_if_missing(
        cursor,
        "reviews",
        "reply_posted_at",
        "DATETIME"
    )
    _add_column_if_missing(
        cursor,
        "reviews",
        "reply_text",
        "TEXT"
    )
    _add_column_if_missing(
        cursor,
        "reviews",
        "replied_at",
        "DATETIME"
    )
    _add_column_if_missing(
        cursor,
        "reviews",
        "reply_error_message",
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
    _add_index_if_missing(
        cursor,
        "reviews",
        "idx_reviews_google_review_id",
        "INDEX idx_reviews_google_review_id (google_review_id)"
    )
    _add_column_if_missing(
        cursor,
        "businesses",
        "use_reviewer_name",
        "BOOLEAN DEFAULT TRUE"
    )
    _add_column_if_missing(
        cursor,
        "businesses",
        "reply_tone",
        "VARCHAR(30) DEFAULT 'professional'"
    )
    _add_column_if_missing(
        cursor,
        "businesses",
        "max_reply_words",
        "INT DEFAULT 120"
    )
    _add_column_if_missing(
        cursor,
        "businesses",
        "auto_generate_replies_for_new_reviews",
        "BOOLEAN DEFAULT TRUE"
    )
    _add_column_if_missing(
        cursor,
        "businesses",
        "auto_post_replies",
        "BOOLEAN DEFAULT FALSE"
    )
    _create_table_if_missing(
        cursor,
        "google_review_reply_logs",
        """
        CREATE TABLE google_review_reply_logs (
            id INT AUTO_INCREMENT PRIMARY KEY,
            review_id INT NOT NULL,
            business_id INT NOT NULL,
            user_id INT NOT NULL,
            google_review_id VARCHAR(255),
            reply_text TEXT NOT NULL,
            status ENUM('posted','failed') NOT NULL,
            error_message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_reply_logs_review_id (review_id),
            INDEX idx_reply_logs_business_id (business_id),
            FOREIGN KEY (review_id)
                REFERENCES reviews(id)
                ON DELETE CASCADE,
            FOREIGN KEY (business_id)
                REFERENCES businesses(id)
                ON DELETE CASCADE,
            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE
        )
        """
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
            google_email VARCHAR(255),
            google_oauth_account_id VARCHAR(255),
            access_token TEXT,
            refresh_token TEXT,
            token_expiry DATETIME,
            scope TEXT,
            scopes TEXT,
            connection_status VARCHAR(50) DEFAULT 'connected',
            is_connected BOOLEAN DEFAULT FALSE,
            connected_at DATETIME,
            disconnected_at DATETIME,
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
        "google_email",
        "VARCHAR(255)"
    )
    _add_column_if_missing(
        cursor,
        "google_business_connections",
        "google_oauth_account_id",
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
    _add_column_if_missing(
        cursor,
        "google_business_connections",
        "connected_at",
        "DATETIME"
    )
    _add_column_if_missing(
        cursor,
        "google_business_connections",
        "disconnected_at",
        "DATETIME"
    )
    _add_index_if_missing(
        cursor,
        "google_business_connections",
        "unique_business_connection",
        "UNIQUE KEY unique_business_connection (user_id, business_id)"
    )
    _create_table_if_missing(
        cursor,
        "google_oauth_attempt_logs",
        """
        CREATE TABLE google_oauth_attempt_logs (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            business_id INT,
            registered_email VARCHAR(255),
            google_email VARCHAR(255),
            status VARCHAR(50) NOT NULL,
            message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_google_oauth_attempt_user (user_id),
            INDEX idx_google_oauth_attempt_business (business_id),
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
        "admin_gbp_override_logs",
        """
        CREATE TABLE admin_gbp_override_logs (
            id INT AUTO_INCREMENT PRIMARY KEY,
            admin_user_id INT NOT NULL,
            business_id INT NULL,
            connected_google_email VARCHAR(255) NOT NULL,
            action VARCHAR(80) NOT NULL DEFAULT 'ADMIN_GBP_OVERRIDE',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_admin_gbp_override_admin (admin_user_id),
            INDEX idx_admin_gbp_override_business (business_id),
            INDEX idx_admin_gbp_override_action (action),
            FOREIGN KEY (admin_user_id)
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
        "google_business_media_uploads",
        """
        CREATE TABLE google_business_media_uploads (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            business_id INT NOT NULL,
            google_connection_id INT NULL,
            google_account_id VARCHAR(255),
            google_location_id VARCHAR(255),
            google_media_name VARCHAR(255),
            category VARCHAR(50) NOT NULL,
            original_filename VARCHAR(255),
            content_type VARCHAR(120),
            file_size_bytes INT DEFAULT 0,
            status ENUM('success','failed') NOT NULL,
            google_url TEXT,
            thumbnail_url TEXT,
            error_message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_google_media_business (business_id, created_at),
            INDEX idx_google_media_user (user_id, created_at),
            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE,
            FOREIGN KEY (business_id)
                REFERENCES businesses(id)
                ON DELETE CASCADE,
            FOREIGN KEY (google_connection_id)
                REFERENCES google_business_connections(id)
                ON DELETE SET NULL
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
    _create_table_if_missing(
        cursor,
        "review_topics",
        """
        CREATE TABLE review_topics (
            id INT AUTO_INCREMENT PRIMARY KEY,
            review_id INT NOT NULL,
            business_id INT NOT NULL,
            topic VARCHAR(120) NOT NULL,
            sentiment ENUM('positive','neutral','negative') NOT NULL DEFAULT 'neutral',
            confidence DECIMAL(5,4) DEFAULT 0.0000,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uniq_review_topic (review_id, topic),
            INDEX idx_review_topics_business_sentiment (business_id, sentiment),
            INDEX idx_review_topics_topic (topic),
            FOREIGN KEY (review_id)
                REFERENCES reviews(id)
                ON DELETE CASCADE,
            FOREIGN KEY (business_id)
                REFERENCES businesses(id)
                ON DELETE CASCADE
        )
        """
    )
    _create_table_if_missing(
        cursor,
        "ai_consultant_reports",
        """
        CREATE TABLE ai_consultant_reports (
            id INT AUTO_INCREMENT PRIMARY KEY,
            business_id INT NOT NULL,
            overall_score DECIMAL(4,2) DEFAULT 0.00,
            health_status VARCHAR(50) NOT NULL,
            executive_summary TEXT,
            strengths JSON,
            weaknesses JSON,
            positive_topics JSON,
            negative_topics JSON,
            priority_actions JSON,
            risks JSON,
            opportunities JSON,
            next_steps JSON,
            raw_ai_response JSON,
            generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_ai_consultant_business_generated (business_id, generated_at),
            FOREIGN KEY (business_id)
                REFERENCES businesses(id)
                ON DELETE CASCADE
        )
        """
    )
    _add_column_if_missing(
        cursor,
        "ai_consultant_reports",
        "review_source",
        "VARCHAR(50) NULL"
    )
    _add_column_if_missing(
        cursor,
        "ai_consultant_reports",
        "report_status",
        "ENUM('up_to_date','outdated') NOT NULL DEFAULT 'up_to_date'"
    )
    _add_column_if_missing(
        cursor,
        "ai_consultant_reports",
        "outdated_at",
        "DATETIME NULL"
    )
    _add_column_if_missing(
        cursor,
        "ai_consultant_reports",
        "daily_briefing",
        "TEXT"
    )
    _add_column_if_missing(
        cursor,
        "ai_consultant_reports",
        "ai_alerts",
        "JSON"
    )
    _add_column_if_missing(
        cursor,
        "ai_consultant_reports",
        "action_plan",
        "JSON"
    )
    _add_column_if_missing(
        cursor,
        "ai_consultant_reports",
        "emotion_breakdown",
        "JSON"
    )
    _add_column_if_missing(
        cursor,
        "ai_consultant_reports",
        "trend_summary",
        "JSON"
    )
    _add_column_if_missing(
        cursor,
        "ai_consultant_reports",
        "latest_attention_reviews",
        "JSON"
    )
    _add_column_if_missing(
        cursor,
        "ai_consultant_reports",
        "last_review_synced_at",
        "DATETIME NULL"
    )
    _create_table_if_missing(
        cursor,
        "consultant_actions",
        """
        CREATE TABLE consultant_actions (
            id INT AUTO_INCREMENT PRIMARY KEY,
            business_id INT NOT NULL,
            report_id INT NULL,
            topic VARCHAR(120) NOT NULL,
            issue_title VARCHAR(255) NOT NULL,
            recommendation TEXT,
            reason TEXT,
            priority VARCHAR(30) DEFAULT 'Medium',
            estimated_impact VARCHAR(50),
            status ENUM('open','in_progress','completed','ignored','verified') DEFAULT 'open',
            owner_note TEXT,
            first_detected_at DATETIME,
            last_detected_at DATETIME,
            resolved_at DATETIME NULL,
            started_at DATETIME NULL,
            completed_at DATETIME NULL,
            ignored_at DATETIME NULL,
            last_detected_review_date DATETIME NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uniq_consultant_action_topic_issue (business_id, topic, issue_title),
            INDEX idx_consultant_actions_business_status (business_id, status),
            INDEX idx_consultant_actions_detected (business_id, last_detected_at),
            FOREIGN KEY (business_id)
                REFERENCES businesses(id)
                ON DELETE CASCADE
        )
        """
    )
    _add_column_if_missing(cursor, "consultant_actions", "report_id", "INT NULL")
    _add_column_if_missing(cursor, "consultant_actions", "reason", "TEXT")
    _add_column_if_missing(cursor, "consultant_actions", "estimated_impact", "VARCHAR(50)")
    _add_column_if_missing(cursor, "consultant_actions", "started_at", "DATETIME NULL")
    _add_column_if_missing(cursor, "consultant_actions", "completed_at", "DATETIME NULL")
    _add_column_if_missing(cursor, "consultant_actions", "ignored_at", "DATETIME NULL")
    _add_column_if_missing(cursor, "consultant_actions", "last_detected_review_date", "DATETIME NULL")
    if _table_exists(cursor, "consultant_actions"):
        cursor.execute(
            """
            UPDATE consultant_actions
            SET status='in_progress'
            WHERE status='planned'
            """
        )
        cursor.execute(
            """
            UPDATE consultant_actions
            SET status='completed',
                completed_at=COALESCE(completed_at, resolved_at, updated_at)
            WHERE status='resolved'
            """
        )
    _modify_column_if_needed(
        cursor,
        "consultant_actions",
        "status",
        "ENUM('open','in_progress','completed','ignored','verified') DEFAULT 'open'"
    )
    _create_table_if_missing(
        cursor,
        "consultant_action_events",
        """
        CREATE TABLE consultant_action_events (
            id INT AUTO_INCREMENT PRIMARY KEY,
            action_id INT NOT NULL,
            business_id INT NOT NULL,
            event_type VARCHAR(80) NOT NULL,
            event_note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_consultant_action_events_action (action_id, created_at),
            FOREIGN KEY (action_id)
                REFERENCES consultant_actions(id)
                ON DELETE CASCADE,
            FOREIGN KEY (business_id)
                REFERENCES businesses(id)
                ON DELETE CASCADE
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
