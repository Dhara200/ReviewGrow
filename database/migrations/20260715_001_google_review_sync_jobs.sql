CREATE TABLE google_review_sync_jobs (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    business_id INT NOT NULL,
    status ENUM('pending','processing','completed','failed') NOT NULL DEFAULT 'pending',
    attempts INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 3,
    next_attempt_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    claimed_by VARCHAR(64) NULL,
    started_at DATETIME NULL,
    heartbeat_at DATETIME NULL,
    completed_at DATETIME NULL,
    error_message TEXT NULL,
    fetched_count INT NOT NULL DEFAULT 0,
    inserted_count INT NOT NULL DEFAULT 0,
    updated_count INT NOT NULL DEFAULT 0,
    topics_inserted INT NOT NULL DEFAULT 0,
    analysis_job_id INT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    active_business_id INT GENERATED ALWAYS AS (
        CASE WHEN status IN ('pending','processing') THEN business_id ELSE NULL END
    ) STORED,
    UNIQUE KEY uniq_google_sync_active_business (active_business_id),
    INDEX idx_google_sync_claim (status, next_attempt_at, created_at),
    INDEX idx_google_sync_user (user_id, created_at),
    INDEX idx_google_sync_business (business_id, created_at),
    CONSTRAINT fk_google_sync_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_google_sync_business FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE CASCADE,
    CONSTRAINT fk_google_sync_analysis_job FOREIGN KEY (analysis_job_id) REFERENCES analysis_jobs(id) ON DELETE SET NULL
);

-- Make Google review writes idempotent before workers are scaled horizontally.
-- Retain the oldest local row if historical concurrent syncs created duplicates.
DELETE duplicate_review
FROM reviews duplicate_review
JOIN reviews retained_review
  ON retained_review.business_id = duplicate_review.business_id
 AND retained_review.google_location_id = duplicate_review.google_location_id
 AND retained_review.google_review_id = duplicate_review.google_review_id
 AND retained_review.id < duplicate_review.id
WHERE duplicate_review.google_review_id IS NOT NULL
  AND duplicate_review.google_location_id IS NOT NULL;

ALTER TABLE reviews
    ADD UNIQUE KEY uniq_reviews_google_location_review (
        business_id,
        google_location_id,
        google_review_id
    );
