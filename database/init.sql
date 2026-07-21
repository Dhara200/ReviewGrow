CREATE DATABASE IF NOT EXISTS reputation_db;

USE reputation_db;

CREATE TABLE users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(50) NOT NULL DEFAULT 'owner',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP
);

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
);

CREATE TABLE businesses (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,

    business_name VARCHAR(255) NOT NULL,
    business_type VARCHAR(100) NOT NULL,

    city VARCHAR(100),
    state VARCHAR(100),
    country VARCHAR(100),
    use_reviewer_name BOOLEAN DEFAULT TRUE,
    reply_tone VARCHAR(30) DEFAULT 'professional',
    max_reply_words INT DEFAULT 120,
    auto_generate_replies_for_new_reviews BOOLEAN DEFAULT TRUE,
    auto_post_replies BOOLEAN DEFAULT FALSE,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    FOREIGN KEY (user_id)
        REFERENCES users(id)
        ON DELETE CASCADE
);

CREATE TABLE reviews (
    id INT AUTO_INCREMENT PRIMARY KEY,

    business_id INT NOT NULL,

    source VARCHAR(100) NOT NULL,

    rating DECIMAL(2,1),

    review_title VARCHAR(255),

    review_text TEXT NOT NULL,

    reviewer_name VARCHAR(255),

    review_date DATETIME,

    analysis_status VARCHAR(50) DEFAULT 'pending',
    sentiment VARCHAR(50),
    category VARCHAR(100),
    complaint_praise_theme VARCHAR(255),
    summary TEXT,
    ai_reply TEXT,
    suggested_reply TEXT,
    google_review_id VARCHAR(255),
    source_platform VARCHAR(50) NOT NULL DEFAULT 'google',
    reply_status ENUM('pending','approved','posted','failed') DEFAULT 'pending',
    reply_generated_at DATETIME,
    reply_posted_at DATETIME,
    reply_text TEXT,
    replied_at DATETIME,
    reply_error_message TEXT,
    confidence_score DECIMAL(5,4),
    analysis_error TEXT,
    analyzed_at DATETIME,
    external_review_id VARCHAR(255),
    google_location_id VARCHAR(255) NULL,
    review_rating DECIMAL(2,1),
    review_created_at DATETIME,
    review_updated_at DATETIME,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_reviews_business_google_location (business_id, google_location_id),
    INDEX idx_reviews_google_review_id (google_review_id),

    FOREIGN KEY (business_id)
        REFERENCES businesses(id)
        ON DELETE CASCADE
);

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
);

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
);

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
);

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
);

CREATE TABLE google_business_connections (
    id INT AUTO_INCREMENT PRIMARY KEY,

    user_id INT NOT NULL,
    business_id INT NOT NULL,

    google_account_id VARCHAR(255),
    google_account_email VARCHAR(255),
    google_location_id VARCHAR(255),
    google_location_name VARCHAR(255),

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
);

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
);

CREATE TABLE reports (
    id INT AUTO_INCREMENT PRIMARY KEY,

    business_id INT NOT NULL,

    summary TEXT,

    top_complaints JSON,

    top_praises JSON,

    recommendations JSON,

    sentiment_score DECIMAL(5,2),

    review_count INT DEFAULT 0,

    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (business_id)
        REFERENCES businesses(id)
        ON DELETE CASCADE
);

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
);

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
    report_status ENUM('up_to_date','outdated') NOT NULL DEFAULT 'up_to_date',
    outdated_at DATETIME NULL,
    daily_briefing TEXT,
    ai_alerts JSON,
    action_plan JSON,
    emotion_breakdown JSON,
    trend_summary JSON,
    latest_attention_reviews JSON,
    last_review_synced_at DATETIME NULL,
    review_source VARCHAR(50) NULL,
    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_ai_consultant_business_generated (business_id, generated_at),
    FOREIGN KEY (business_id)
        REFERENCES businesses(id)
        ON DELETE CASCADE
);

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
);

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
);

CREATE TABLE subscriptions (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    plan_name VARCHAR(50) NOT NULL,
    status ENUM('active','expired','cancelled','disabled') DEFAULT 'expired',
    subscription_start_date DATETIME NULL,
    subscription_end_date DATETIME NULL,
    review_credits INT DEFAULT 500,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_subscriptions_user_id (user_id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE payments (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    subscription_id INT NULL,
    plan_code VARCHAR(50) NULL,
    amount DECIMAL(10,2) NOT NULL,
    amount_paise BIGINT UNSIGNED NULL,
    currency VARCHAR(10) DEFAULT 'INR',
    payment_method VARCHAR(50) DEFAULT 'UPI',
    payment_status ENUM('pending','success','failed','rejected','created','attempted','paid','refunded','needs_review') DEFAULT 'pending',
    transaction_id VARCHAR(255) NOT NULL,
    payment_gateway VARCHAR(50) DEFAULT 'manual_upi',
    razorpay_order_id VARCHAR(255) NULL,
    razorpay_payment_id VARCHAR(255) NULL,
    paid_at DATETIME NULL,
    failure_code VARCHAR(100) NULL,
    failure_reason VARCHAR(255) NULL,
    processed_at DATETIME NULL,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uniq_payments_razorpay_order_id (razorpay_order_id),
    UNIQUE KEY uniq_payments_razorpay_payment_id (razorpay_payment_id),
    INDEX idx_payments_user_status (user_id, payment_status),
    INDEX idx_payments_created_at (created_at),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE SET NULL
);
