-- Reusable atomic rate-limiter state. All application writes use UTC_TIMESTAMP(6).

CREATE TABLE rate_limit_counters (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    scope VARCHAR(64) NOT NULL,
    key_hash BINARY(32) NOT NULL,
    window_started_at DATETIME(6) NOT NULL,
    attempt_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
    blocked_until DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)),
    updated_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)),
    PRIMARY KEY (id),
    UNIQUE KEY uniq_rate_limit_scope_key (scope, key_hash),
    KEY idx_rate_limit_updated_at (updated_at)
) ENGINE=InnoDB;
