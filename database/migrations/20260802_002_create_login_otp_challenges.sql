-- Password-login email OTP challenges. OTP plaintext is never stored here.

-- Phase 2 standardizes queue priority: lower numbers are processed first.
ALTER TABLE email_jobs ALTER COLUMN priority SET DEFAULT 100;

CREATE TABLE login_otp_challenges (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id INT NOT NULL,
    otp_hash VARCHAR(255) NOT NULL,
    expires_at DATETIME(6) NOT NULL,
    attempt_count SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    max_attempts SMALLINT UNSIGNED NOT NULL DEFAULT 5,
    used_at DATETIME(6) NULL,
    invalidated_at DATETIME(6) NULL,
    requested_ip VARCHAR(45) NOT NULL,
    user_agent_hash BINARY(32) NOT NULL,
    resend_count SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    last_sent_at DATETIME(6) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)),
    updated_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)),
    PRIMARY KEY (id),
    KEY idx_login_otp_user_created (user_id, created_at),
    KEY idx_login_otp_expiry (expires_at),
    KEY idx_login_otp_active (user_id, used_at, invalidated_at, expires_at),
    KEY idx_login_otp_rate_user (user_id, last_sent_at),
    KEY idx_login_otp_rate_ip (requested_ip, created_at),
    CONSTRAINT fk_login_otp_user FOREIGN KEY (user_id) REFERENCES users(id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
