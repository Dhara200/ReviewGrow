USE reputation_db;

ALTER TABLE reviews ADD COLUMN IF NOT EXISTS category VARCHAR(100) NULL;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS complaint_praise_theme VARCHAR(255) NULL;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS suggested_reply TEXT NULL;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS confidence_score DECIMAL(5,4) NULL;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS analysis_error TEXT NULL;

CREATE TABLE IF NOT EXISTS analysis_jobs (
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

CREATE TABLE IF NOT EXISTS ai_usage_logs (
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

CREATE TABLE IF NOT EXISTS ai_monthly_usage (
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
