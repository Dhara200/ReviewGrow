USE reputation_db;

CREATE TABLE IF NOT EXISTS google_business_performance (
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
);
