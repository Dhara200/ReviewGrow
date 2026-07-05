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
