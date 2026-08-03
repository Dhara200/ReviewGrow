-- Durable competitor refresh jobs and one UTC snapshot per subject per day.
ALTER TABLE analysis_jobs
    MODIFY COLUMN job_type ENUM('review_analysis','ai_consultant','competitor_refresh_all')
        NOT NULL DEFAULT 'review_analysis',
    ADD COLUMN refresh_window_key VARCHAR(255) NULL AFTER operation_key,
    ADD COLUMN result_json JSON NULL AFTER result_consultant_report_id,
    ADD UNIQUE KEY uniq_analysis_jobs_refresh_window (refresh_window_key);

CREATE TABLE business_reputation_snapshots (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    business_id INT NOT NULL,
    subject_type ENUM('customer','competitor') NOT NULL,
    subject_key VARCHAR(255) NOT NULL,
    competitor_id INT NULL,
    google_place_id VARCHAR(255) NOT NULL,
    subject_name VARCHAR(255) NOT NULL,
    rating DECIMAL(2,1) NULL,
    user_rating_count INT NULL,
    business_status VARCHAR(50) NULL,
    captured_at DATETIME(6) NOT NULL,
    capture_date DATE NOT NULL,
    source VARCHAR(50) NOT NULL DEFAULT 'google_places',
    refresh_job_id INT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uniq_reputation_snapshot_daily (business_id, subject_key, capture_date),
    KEY idx_reputation_snapshots_business_date (business_id, capture_date),
    KEY idx_reputation_snapshots_competitor_date (competitor_id, capture_date),
    KEY idx_reputation_snapshots_job (refresh_job_id),
    CONSTRAINT fk_reputation_snapshots_business
        FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE,
    CONSTRAINT fk_reputation_snapshots_competitor
        FOREIGN KEY (competitor_id) REFERENCES business_competitors(id) ON DELETE CASCADE,
    CONSTRAINT fk_reputation_snapshots_refresh_job
        FOREIGN KEY (refresh_job_id) REFERENCES analysis_jobs(id) ON DELETE SET NULL
);
