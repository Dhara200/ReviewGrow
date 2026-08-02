-- Durable transactional-email queue. All application timestamps are UTC.

CREATE TABLE email_jobs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id INT NULL,
    recipient_email VARCHAR(254) NOT NULL,
    email_type VARCHAR(64) NOT NULL,
    template_name VARCHAR(128) NOT NULL,
    template_data JSON NOT NULL,
    priority SMALLINT NOT NULL DEFAULT 0,
    status ENUM('pending','processing','sent','failed','cancelled') NOT NULL DEFAULT 'pending',
    attempt_count SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    max_attempts SMALLINT UNSIGNED NOT NULL DEFAULT 6,
    next_attempt_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)),
    deduplication_key VARCHAR(191) NULL,
    ses_message_id VARCHAR(255) NULL,
    last_error VARCHAR(500) NULL,
    processing_started_at DATETIME(6) NULL,
    sent_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)),
    updated_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)),
    PRIMARY KEY (id),
    UNIQUE KEY uniq_email_jobs_deduplication_key (deduplication_key),
    KEY idx_email_jobs_pending (status, next_attempt_at, priority, created_at),
    KEY idx_email_jobs_user (user_id),
    CONSTRAINT fk_email_jobs_user FOREIGN KEY (user_id) REFERENCES users(id)
        ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
